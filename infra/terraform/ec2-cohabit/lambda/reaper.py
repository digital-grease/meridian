"""Stop the cohabit instance when meridian left it running.

Why this exists
---------------
scripts/run-weekly.sh stops the instance itself, on every exit path it
can reach. The words "it can reach" are the whole problem: the stop is a
function call at the end of each branch, not a trap, and even as a trap
it would not survive SIGKILL. Anything that kills the wrapper outright
skips it.

2026-W34 is the worked example. SSM's AWS-RunShellScript document
carries a 3600 s default executionTimeout that the orchestrator never
overrode, so at exactly one hour SSM SIGKILLed a run that needed about
2h10m. No manifest was written, and self_stop_if_needed never executed.
The g5.2xlarge then sat at load 0.00 with an empty GPU for eighteen
hours until a human happened to look at the console. At roughly
$1.21/hour against a project budget of about $45/month, that single
missed stop cost around half a month of budget.

The executionTimeout is fixed in orchestrator.py, which makes that
particular kill unlikely. It does not make the class of failure go away:
an OOM kill, a kernel panic, a spot-style reclaim of the SSM agent, or
the next unexamined AWS default all land in the same place. The wrapper
cannot be the only thing that stops the instance, because the wrapper is
the thing that might not be running.

What makes this safe to run unattended
--------------------------------------
The instance is shared with specter, and specter's work is interactive
GPU work that meridian must never interrupt. A naive "stop it if the CPU
looks quiet" reaper would eventually kill someone's live session, and
one such incident would rightly get the whole mechanism turned off.

So the gate is ownership, not idleness. The question asked here is not
"does this instance look busy" but "did MERIDIAN start this boot, and is
meridian's run over". Those have precise answers:

  * The orchestrator dispatches every run through SSM with a fixed
    Comment. An invocation carrying that comment, requested after the
    current LaunchTime, means meridian started this boot. (LaunchTime
    updates on every start, not just the original launch, so it dates
    the boot rather than the instance.)
  * If that invocation is still Pending / InProgress / Delayed, the run
    is alive and nothing here should touch it.
  * If it reached a terminal status, meridian is done and the wrapper
    should already have stopped the box. It has not. That is the bug
    this function exists to clean up after.
  * If there is NO meridian invocation for this boot, somebody else
    started the instance. That is specter's session and this function
    leaves it strictly alone.

An idleness probe still runs before the stop, but only as a second
gate, never as the first: it catches the narrow race where a human
started GPU work on the box after meridian's run finished but before
this function noticed.

Accepted gap
------------
If the SSM probe cannot run at all (agent down, instance unreachable)
this function ALERTS AND DOES NOT STOP, even though every ownership
check has already passed. That is deliberate and it is the expensive
choice: the instance keeps billing until a human reads the alert.

The alternative is stopping a box whose state we cannot observe, and the
asymmetry is not close. A wrongly-continued instance costs about a
dollar an hour and pages someone immediately. A wrongly-stopped instance
destroys uncommitted work belonging to a person who did not opt into
this automation. Set STOP_WHEN_PROBE_UNAVAILABLE=1 to invert this, and
be sure about it first.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

import boto3

_log = logging.getLogger()
_log.setLevel(logging.INFO)

ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")
sns = boto3.client("sns")

INSTANCE_ID = os.environ["INSTANCE_ID"]
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]

# Must match the Comment the orchestrator sends. This string is the
# ownership signal, so a drift between the two files turns every reap
# into a no-op that reports "not meridian's boot" forever. The
# orchestrator's Comment is asserted against this value in the tests.
DISPATCH_COMMENT_MATCH = os.environ.get(
    "DISPATCH_COMMENT_MATCH", "meridian weekly pipeline"
)

# Grace period after boot before this function will judge anything.
#
# Guards the startup race: the orchestrator starts the instance, then
# waits for InstanceStatusOk (up to 600 s) before it dispatches, so
# there is a window where the box is running with no meridian
# invocation against it yet and looks exactly like a specter boot. A
# reap in that window would kill the weekly run at its most fragile
# moment. 45 minutes clears the readiness wait several times over and
# costs at most one extra hour of billing on the failure path this
# function is cleaning up after.
MIN_UPTIME_SECONDS = int(os.environ.get("MIN_UPTIME_SECONDS", "2700") or "2700")

# Same threshold and the same meaning as the wrapper's pre-flight, so
# "busy" means one thing in this system rather than two.
GPU_MEMORY_THRESHOLD_MB = int(
    os.environ.get("GPU_MEMORY_THRESHOLD_MB", "500") or "500"
)

PROBE_TIMEOUT_SECONDS = int(os.environ.get("PROBE_TIMEOUT_SECONDS", "60") or "60")

STOP_WHEN_PROBE_UNAVAILABLE = (
    os.environ.get("STOP_WHEN_PROBE_UNAVAILABLE", "0").strip().lower()
    in ("1", "true", "yes")
)

DRY_RUN = os.environ.get("DRY_RUN", "0").strip().lower() in ("1", "true", "yes")

# SSM statuses that mean the command has not finished. Anything outside
# this set is terminal. Listed explicitly rather than as "not in
# {Success, Failed}" because SSM has more terminal states than people
# remember (Cancelled, TimedOut, DeliveryTimedOut, Undeliverable,
# Terminated) and treating an unknown status as active would make this
# function fail open, i.e. never reap.
ACTIVE_SSM_STATUSES = frozenset({"Pending", "InProgress", "Delayed"})

# The probe. Deliberately tiny and read-only: it prints two facts and
# exits 0 whatever it finds, so a busy box and an idle box are told
# apart by parsing stdout rather than by an exit code that could also
# mean the shell broke.
PROBE_SCRIPT = r"""
used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1)
[ -z "$used" ] && used=unknown
echo "GPU_USED_MB=$used"
procs=$(pgrep -fa 'specter|run-weekly|meridian' 2>/dev/null | grep -v pgrep | wc -l)
echo "BUSY_PROCS=$procs"
"""


def _publish(subject: str, body: str) -> None:
    # Subjects stay printable ASCII with no em-dashes. SNS rejects
    # anything else and the failure surfaces only as a log line, which
    # would silently drop precisely the alerts about a GPU instance
    # left billing.
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=f"[meridian] {subject}", Message=body)
    except Exception:  # broad on purpose: alerting is best effort
        _log.exception("SNS publish failed for: %s", subject)


def _describe() -> tuple[str, dt.datetime | None]:
    resp = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
    for reservation in resp.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            return inst["State"]["Name"], inst.get("LaunchTime")
    return "unknown", None


def _meridian_invocations(launch_time: dt.datetime) -> list[dict]:
    """Meridian dispatches belonging to the CURRENT boot, newest first."""
    invocations: list[dict] = []
    token: str | None = None
    while True:
        kwargs = {"InstanceId": INSTANCE_ID, "MaxResults": 50}
        if token:
            kwargs["NextToken"] = token
        resp = ssm.list_command_invocations(**kwargs)
        invocations.extend(resp.get("CommandInvocations", []))
        token = resp.get("NextToken")
        if not token:
            break

    mine = [
        inv
        for inv in invocations
        if DISPATCH_COMMENT_MATCH in (inv.get("Comment") or "")
        and inv.get("RequestedDateTime") is not None
        and inv["RequestedDateTime"] >= launch_time
    ]
    mine.sort(key=lambda i: i["RequestedDateTime"], reverse=True)
    return mine


def _probe_is_idle() -> tuple[bool | None, str]:
    """(True idle, False busy, None unavailable) plus a human detail."""
    try:
        sent = ssm.send_command(
            InstanceIds=[INSTANCE_ID],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [PROBE_SCRIPT],
                "executionTimeout": [str(max(PROBE_TIMEOUT_SECONDS, 30))],
            },
            TimeoutSeconds=60,
            Comment="meridian reaper idleness probe",
        )
        command_id = sent["Command"]["CommandId"]
    except Exception as exc:  # any failure here means "cannot observe", not "idle"
        return None, f"probe could not be dispatched: {exc}"

    waiter_cfg = {
        "Delay": 3,
        "MaxAttempts": max(PROBE_TIMEOUT_SECONDS // 3, 5),
    }
    try:
        ssm.get_waiter("command_executed").wait(
            CommandId=command_id,
            InstanceId=INSTANCE_ID,
            WaiterConfig=waiter_cfg,
        )
    except Exception as exc:  # the waiter raises on any non-Success terminal status
        # The waiter also raises on a non-Success terminal status, so
        # fall through and read the invocation rather than trusting it.
        _log.warning("probe waiter did not settle cleanly: %s", exc)

    try:
        inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=INSTANCE_ID)
    except Exception as exc:  # unreadable result is "cannot observe", not "idle"
        return None, f"probe result unreadable: {exc}"

    if inv.get("Status") != "Success":
        return None, f"probe did not succeed: {inv.get('Status')} {inv.get('StatusDetails')}"

    out = inv.get("StandardOutputContent") or ""
    gpu_raw = ""
    procs_raw = ""
    for line in out.splitlines():
        if line.startswith("GPU_USED_MB="):
            gpu_raw = line.split("=", 1)[1].strip()
        elif line.startswith("BUSY_PROCS="):
            procs_raw = line.split("=", 1)[1].strip()

    if not gpu_raw or not procs_raw:
        return None, f"probe output not parseable: {out[:200]!r}"

    try:
        procs = int(procs_raw)
    except ValueError:
        return None, f"probe process count not an integer: {procs_raw!r}"

    # An unreadable GPU is not an idle GPU. nvidia-smi missing or failing
    # on a g5 means something is wrong with the box, and guessing "idle"
    # there is how a reaper eventually kills live work.
    if gpu_raw == "unknown":
        return None, "probe could not read GPU memory (nvidia-smi unavailable)"

    try:
        gpu_mb = int(gpu_raw)
    except ValueError:
        return None, f"probe GPU value not an integer: {gpu_raw!r}"

    detail = f"GPU {gpu_mb} MB used (threshold {GPU_MEMORY_THRESHOLD_MB}), {procs} matching process(es)"
    if gpu_mb > GPU_MEMORY_THRESHOLD_MB or procs > 0:
        return False, detail
    return True, detail


def lambda_handler(event: dict, context: object) -> dict:
    _log.info("reaper start: event=%s", event)

    state, launch_time = _describe()
    if state != "running":
        _log.info("instance is %s; nothing to do", state)
        return {"status": "noop", "reason": f"instance {state}"}

    if launch_time is None:
        _log.warning("running instance reported no LaunchTime; refusing to judge")
        return {"status": "noop", "reason": "no launch time"}

    now = dt.datetime.now(dt.UTC)
    uptime = (now - launch_time).total_seconds()
    if uptime < MIN_UPTIME_SECONDS:
        _log.info("uptime %.0fs below grace %ds; too early", uptime, MIN_UPTIME_SECONDS)
        return {"status": "noop", "reason": "within startup grace", "uptime_s": int(uptime)}

    mine = _meridian_invocations(launch_time)
    if not mine:
        # Somebody else's boot. This is the specter case and it is the
        # single most important branch in the file: leaving it alone is
        # what makes the whole mechanism safe to leave switched on.
        _log.info("no meridian dispatch since boot; not meridian's instance to stop")
        return {"status": "noop", "reason": "not meridian's boot", "uptime_s": int(uptime)}

    latest = mine[0]
    status = latest.get("Status", "unknown")
    if status in ACTIVE_SSM_STATUSES:
        _log.info("meridian run still %s; leaving instance up", status)
        return {"status": "noop", "reason": f"run {status}", "uptime_s": int(uptime)}

    # Meridian owns this boot and its run is terminal. The wrapper should
    # have stopped the instance and did not.
    idle, detail = _probe_is_idle()
    if idle is False:
        _log.warning("instance busy despite terminal meridian run: %s", detail)
        _publish(
            "ATTENTION: instance running after meridian finished, but it is busy",
            f"The meridian run on {INSTANCE_ID} ended with status {status}, so the wrapper "
            f"should have stopped the instance and did not. The reaper did NOT stop it "
            f"because the box is currently busy: {detail}.\n\n"
            f"That is most likely specter work started after the meridian run finished, in "
            f"which case nothing is wrong except that meridian's self-stop failed. Check, "
            f"and stop it by hand when the box is free:\n\n"
            f"  aws ec2 stop-instances --instance-ids {INSTANCE_ID}\n",
        )
        return {"status": "noop", "reason": "busy", "detail": detail}

    if idle is None and not STOP_WHEN_PROBE_UNAVAILABLE:
        _log.error("probe unavailable; alerting instead of stopping: %s", detail)
        _publish(
            "ATTENTION: instance still running, reaper could not verify it is idle",
            f"The meridian run on {INSTANCE_ID} ended with status {status} and the instance "
            f"is still running after {uptime / 3600:.1f} hours, so the wrapper's self-stop "
            f"did not happen.\n\n"
            f"The reaper did NOT stop it because it could not confirm the box is idle: "
            f"{detail}\n\n"
            f"It refuses to stop an instance it cannot observe, because the cost of being "
            f"wrong is somebody's uncommitted GPU work rather than about a dollar an hour. "
            f"This needs a human. Confirm nothing is running, then:\n\n"
            f"  aws ec2 stop-instances --instance-ids {INSTANCE_ID}\n",
        )
        return {"status": "alerted", "reason": "probe unavailable", "detail": detail}

    if DRY_RUN:
        _log.info("DRY_RUN set; would have stopped %s (%s)", INSTANCE_ID, detail)
        return {"status": "dry_run", "detail": detail, "uptime_s": int(uptime)}

    ec2.stop_instances(InstanceIds=[INSTANCE_ID])
    hours = uptime / 3600
    _log.info("stopped %s after %.1fh (%s)", INSTANCE_ID, hours, detail)
    _publish(
        "reaper stopped an instance the weekly run left running",
        f"{INSTANCE_ID} was still running {hours:.1f} hours after boot with meridian's own "
        f"run already terminal (status {status}), so scripts/run-weekly.sh did not reach its "
        f"self-stop. The reaper verified the box was idle ({detail}) and stopped it.\n\n"
        f"This is a cleanup, not a success. Something killed the wrapper before it could stop "
        f"its own instance, and that cause is still there. Check the SSM invocation and "
        f"/data/meridian/logs/run-weekly.log for what happened to this run.\n",
    )
    return {
        "status": "stopped",
        "uptime_s": int(uptime),
        "run_status": status,
        "detail": detail,
    }
