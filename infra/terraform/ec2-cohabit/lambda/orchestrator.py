"""Meridian weekly-pipeline orchestrator (AWS Lambda).

Triggered by EventBridge Scheduler (Mon 04:00 America/Chicago by
default). Cohabits specter's g5.2xlarge.

Architecture is fire-and-forget by necessity — Lambda's 15-minute hard
timeout cannot wait out a 30-90 minute pipeline run. So this Lambda's
only job is:

  1. Try to start the instance, retrying transient capacity errors.
  2. If it was already running, the cohabit policy says we defer (no
     backfill) — publish a 'deferred' alert and exit.
  3. Otherwise wait for the instance to become SSM-reachable.
  4. Send the wrapper script via SSM and exit immediately.

Every exit path alerts, and every terminal exit path also files a
run-log record for the week it failed to sample. 2026-W30 and 2026-W31
were lost because `ec2.start_instances` raised
InsufficientInstanceCapacity straight out of the handler: the run never
happened, SNS said nothing, and the only signal was the publish workflow
404ing six hours later. Two weeks went by before anyone noticed, and the
append-only run log still has no line for either week, so the gap is not
reproducible from the published data. Nothing here may fail quietly
again, and nothing here may fail without leaving a record.

The wrapper script (deployed in Phase 3 at the path in
WRAPPER_SCRIPT_PATH) takes over from there: pre-flight contention
check, secret fetch from SSM Parameter Store, pipeline run, SNS alert
on outcome, and `ec2:StopInstances` on itself if WE_OWN_LIFECYCLE=1.

Environment variables (set by the Terraform module):
  INSTANCE_ID:                 specter's EC2 instance id
  WRAPPER_SCRIPT_PATH:         absolute path of the wrapper on the instance
  SNS_TOPIC_ARN:               for deferral / orchestrator-side failure alerts
  SSM_COMMAND_TIMEOUT_SECONDS: SSM SendCommand *delivery* deadline
  ARCHIVE_BUCKET:              S3 archive bucket, for the failure run-log
                               record (optional; skipped when empty)
  ARCHIVE_BUCKET_PREFIX:       key prefix inside that bucket
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

_log = logging.getLogger()
_log.setLevel(logging.INFO)

INSTANCE_ID = os.environ["INSTANCE_ID"]
WRAPPER_SCRIPT_PATH = os.environ["WRAPPER_SCRIPT_PATH"]
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]

# SSM SendCommand's TimeoutSeconds is a DELIVERY deadline: how long SSM
# keeps trying to hand the command to the agent before marking it
# DeliveryTimedOut. It does not bound execution, so it can never be a run
# budget (that lives in the wrapper). Until 2026-08 this env var was set
# by Terraform and read by nobody, and the call below hardcoded 600, so an
# operator raising it to "extend the run" got a clean apply and no change.
# Read it here so the knob is real, and keep the default at the old
# hardcoded value so wiring it changes nothing on its own.
SSM_COMMAND_TIMEOUT_SECONDS = int(
    os.environ.get("SSM_COMMAND_TIMEOUT_SECONDS", "600") or "600"
)

# Optional: where to drop a run-log record when a Monday dies before the
# pipeline ever starts. Empty means "not configured", and the record is
# logged to CloudWatch only.
ARCHIVE_BUCKET = os.environ.get("ARCHIVE_BUCKET", "")
ARCHIVE_BUCKET_PREFIX = os.environ.get("ARCHIVE_BUCKET_PREFIX", "meridian/")

# How long to wait for InstanceStatusOk. Cold-boot of a g5 + the AWS
# SSM agent reaching healthy is usually ~3 min; 10 min is a comfortable cap.
INSTANCE_READY_TIMEOUT_SECONDS = 600
INSTANCE_READY_POLL_INTERVAL = 15

# A capacity error means AWS has no g5.2xlarge free in our AZ *right
# now* — transient, not permanent. Relocating isn't a lever we have:
# we cohabit specter's instance, and a stopped instance is pinned to
# its subnet, so another AZ or instance type would mean a different
# box than the one this whole design shares. Retrying is the only
# option. botocore already retries these internally, but it exhausts
# four attempts in ~8 seconds, which is far too narrow to ride out a
# real capacity crunch.
RETRYABLE_START_ERRORS = frozenset({
    "InsufficientInstanceCapacity",
    "InsufficientHostCapacity",
    "InsufficientReservedInstanceCapacity",
})

# In-Lambda retry budget for the start call. Deliberately modest: the
# 900 s function timeout has to cover this *plus* the up-to-600 s
# readiness wait.
#
# What rides out a longer outage is the async-invoke retry configured on
# `aws_lambda_function_event_invoke_config` in lambda.tf, NOT the
# EventBridge Scheduler retry_policy. This comment used to say the
# opposite, and that error is part of why 2026-W30 and 2026-W31 were
# lost: Scheduler invokes Lambda asynchronously and gets a 202 the
# instant Lambda accepts the event, so a handler that raises still looks
# like a successful delivery to Scheduler and its retry never fires
# (CloudWatch recorded exactly one Invocation on each of those Mondays).
# scheduler.tf was corrected in 8b30b78; this comment was not.
#
# The honest coverage the async retries buy is minutes, not hours: 2
# retries, with Lambda-side backoff of roughly 1 minute and then 2
# minutes, so three attempts each capped by START_RETRY_MAX_SECONDS add
# up to about 11 minutes in practice and 15 at the absolute outside.
# A multi-hour capacity crunch is not survivable at this layer. For that
# the SNS alert plus the manual re-fire in scripts/ec2-runbook.md is the
# recovery path, and the week may genuinely be lost.
START_RETRY_MAX_SECONDS = 240
START_RETRY_INITIAL_BACKOFF = 15
START_RETRY_MAX_BACKOFF = 120

# How much of the budget one StartInstances call is assumed to consume.
# botocore does its own four internal retries on a capacity error and
# burns ~8 s doing it, which is visible in the live traces as the budget
# dropping from 240 to 232 before our first sleep. We keep this much in
# reserve so the loop always has room for one more real attempt.
EXPECTED_CALL_SECONDS = 10

# Leave room at the end of the invocation to publish an alert before
# Lambda kills us — a timeout death is another silent failure.
HANDLER_RESERVE_SECONDS = 20

# Fallback when no Lambda context is supplied (direct invocation in
# tests); matches the `timeout` in lambda.tf.
LAMBDA_MAX_RUNTIME_SECONDS = 900


class CapacityUnavailable(RuntimeError):
    """AWS had no capacity for the instance within our retry budget."""

ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")
sns = boto3.client("sns")
s3 = boto3.client("s3")


def _alert(subject: str, body: dict[str, Any]) -> None:
    """Best-effort SNS publish. Swallow publish errors: CloudWatch logs
    are the primary signal; SNS is a notification convenience.

    Logged at ERROR, not WARNING, because a swallowed publish is exactly
    the shape of the 2026-W30/W31 silence. The Lambda Errors alarm in
    sns.tf is the backstop that does not depend on this call succeeding.

    Keep every Subject printable ASCII. SNS documents Subject that way,
    rejects anything else with an InvalidParameter ClientError, and this
    function swallows it, so a non-ASCII subject is an alert that never
    arrives and never complains.
    """
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"[meridian] {subject}"[:100],
            Message=json.dumps(body, indent=2, default=str),
        )
    except ClientError as e:
        _log.error("SNS publish failed (subject=%r): %s", subject, e)


def _target_week_id(now: datetime | None = None) -> str:
    """ISO week the run would have sampled.

    The schedule fires Monday 04:00 America/Chicago, i.e. 09:00 or 10:00
    UTC on a Monday, and samples the week that just ended. Mirrors
    `date -u --date='yesterday' +'%G-W%V'` in scripts/run-weekly.sh, which
    is the value the pipeline itself writes into the run log. Keep the two
    in step or a failure record will be filed against the wrong week.
    """
    ts = (now or datetime.now(timezone.utc)) - timedelta(days=1)
    iso = ts.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _record_failed_run(week_id: str, failure_reason: str, error: BaseException) -> None:
    """Write a run-log entry for a week that died before sampling started.

    Why this exists: the append-only run log has no entry at all for
    2026-W30 or 2026-W31. The outage lives only as prose on the
    methodology page, so the gap is not reproducible from the published
    data, and check_run_health.py cannot see a week that never wrote a
    line. A Monday that fails this early has to record itself.

    Shape matches meridian.pipeline.run_log.RunLogEntry exactly. That
    matters: read_run_log() does `RunLogEntry(**obj)`, so any extra
    top-level key raises TypeError and breaks a reader over a record
    that is retained forever. That is why the failure reason travels in
    `note` and in `errors[0].message` rather than in a `failure_reason`
    key of its own.

    Destination is the S3 archive, not `data/run_log.jsonl`: the Lambda
    has no access to the repo, and the real log lives on the instance
    that never came up. The key is fixed per week and the bucket is
    versioned, so a Monday that fails on all three async attempts leaves
    one current record plus its earlier attempts as non-current versions
    rather than three competing lines. If a later retry succeeded, the
    pipeline's own entry for that week supersedes this one; reconcile
    before appending anything to the public log.

    THE WHOLE BODY IS INSIDE A CATCH-ALL, and that is the load-bearing
    part. This runs BEFORE _alert() on every terminal path, so anything
    that escapes it takes the alert with it. The inner handler used to
    guard only ClientError, which covers an AccessDenied from S3 and
    nothing else: EndpointConnectionError, ConnectTimeoutError,
    ReadTimeoutError, NoCredentialsError and ParamValidationError are all
    botocore exceptions that are NOT ClientError, and any of them
    propagated straight out of the `except CapacityUnavailable` block in
    lambda_handler. A capacity exhaustion then published zero SNS
    messages, and the readiness-timeout path additionally skipped its
    _stop_if_ours, leaving a g5.2xlarge running at roughly $1/hour with
    nothing said. That is the exact 2026-W30/W31 silence, reintroduced by
    the bookkeeping added to prevent it. Bookkeeping never gets to be the
    reason an alert did not go out.
    """
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = {
            "started_at": now,
            "finished_at": now,
            "week_id": week_id,
            "host": os.environ.get(
                "AWS_LAMBDA_FUNCTION_NAME", "meridian-orchestrator"
            ),
            "pid": os.getpid(),
            # No PipelineConfig was ever loaded: nothing got as far as
            # reading config.yaml. An empty hash is the honest value.
            "config_hash": "",
            "runners": [],
            "total_samples_written": 0,
            "pairs_complete": 0,
            "pairs_skipped": 0,
            "pairs_failed": 0,
            "per_runner_samples": {},
            "estimated_cost_usd": 0.0,
            "actual_cost_usd": 0.0,
            "errors": [
                {
                    "provider": "aws",
                    "model_id": f"ec2/{INSTANCE_ID}",
                    "prompt_id": "-",
                    "error_type": type(error).__name__,
                    "message": str(error)[:500],
                }
            ],
            "note": f"orchestrator terminal failure: {failure_reason}",
            "unusable_samples": {},
        }
        line = json.dumps(entry, sort_keys=True)
        # Log it unconditionally. CloudWatch retention is 90 days
        # (lambda.tf), which is long enough for the record to be
        # recoverable by hand even if the S3 write below fails or the
        # bucket is not configured.
        _log.error("run_log failure entry: %s", line)

        if not ARCHIVE_BUCKET:
            _log.warning("ARCHIVE_BUCKET unset; failure entry logged only")
            return
        key = f"{ARCHIVE_BUCKET_PREFIX}run_log/failures/{week_id}.json"
        try:
            s3.put_object(
                Bucket=ARCHIVE_BUCKET,
                Key=key,
                Body=(line + "\n").encode("utf-8"),
                ContentType="application/json",
            )
            _log.info("wrote failure run-log record to s3://%s/%s", ARCHIVE_BUCKET, key)
        except ClientError as e:
            # Never let bookkeeping mask the failure it is describing.
            _log.error("failed to write failure run-log record to s3: %s", e)
    except Exception:
        # Deliberately broad, deliberately last. See the docstring: the
        # caller's next statement is the SNS alert, and nothing in here
        # is worth losing it over.
        _log.exception(
            "failure run-log bookkeeping raised for %s; continuing to alert",
            week_id,
        )


def _start_or_defer(deadline: float) -> str | None:
    """Return previous state, or None to signal 'defer — instance was running'.

    Retries capacity errors with exponential backoff until `deadline`
    (a time.monotonic() value). Raises CapacityUnavailable if the
    budget runs out with capacity still unavailable; any other
    ClientError propagates untouched, since retrying a permissions or
    malformed-request error just wastes the budget.

    The backoff is CLAMPED to the remaining budget rather than compared
    against it. The old code gave up as soon as the next backoff was
    larger than what was left, which threw away the tail of every
    budget: the 2026-08-10 trace retried at 15 s, 30 s and 60 s and then
    declared the budget exhausted at 145 s of 240 s, leaving ~95 s
    unspent, and made START_RETRY_MAX_BACKOFF=120 unreachable (it needs
    a ~265 s budget to be selected at all). Clamping spends the whole
    budget and only gives up when there is not even room for one more
    call.
    """
    backoff = START_RETRY_INITIAL_BACKOFF
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = ec2.start_instances(InstanceIds=[INSTANCE_ID])
            break
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code not in RETRYABLE_START_ERRORS:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= EXPECTED_CALL_SECONDS:
                _log.error(
                    "capacity unavailable after %d attempt(s); budget exhausted "
                    "(%.0fs left, need %ds for another call)",
                    attempt, remaining, EXPECTED_CALL_SECONDS,
                )
                raise CapacityUnavailable(
                    f"{code} starting {INSTANCE_ID} after {attempt} attempt(s)"
                ) from e
            sleep_for = min(backoff, remaining - EXPECTED_CALL_SECONDS)
            _log.warning(
                "attempt %d: %s, retrying in %.0fs (%.0fs budget left)",
                attempt, code, sleep_for, remaining,
            )
            time.sleep(sleep_for)
            backoff = min(backoff * 2, START_RETRY_MAX_BACKOFF)

    if attempt > 1:
        # Say so out loud. On 2026-08-10 the operator got a "capacity
        # unavailable, instance did not start" email and no follow-up,
        # and only found out from the run artifacts that the retry had
        # worked three minutes later.
        _alert(f"recovered on retry {attempt - 1}, instance started", {
            "instance_id": INSTANCE_ID,
            "attempts": attempt,
            "reason": (
                "StartInstances hit a transient capacity error and then "
                "succeeded within this invocation's retry budget. No data "
                "was lost, no action needed."
            ),
        })

    state_change = resp["StartingInstances"][0]
    prev = state_change["PreviousState"]["Name"]
    cur = state_change["CurrentState"]["Name"]
    _log.info("ec2.start_instances: previous=%s current=%s", prev, cur)

    if prev == "running":
        # ASCII only in the subject. SNS documents Subject as printable
        # ASCII, and _alert swallows a rejected publish, so a subject with
        # an em-dash in it is an alert that silently never arrives. This
        # one is the deferral notice, the single most frequently taken
        # alert path in the function.
        _alert("deferred, instance already running", {
            "instance_id": INSTANCE_ID,
            "previous_state": prev,
            "current_state": cur,
            "reason": (
                "Specter or another workload is on the instance. Per the "
                "no-backfill policy this week's local-baseline cell stays "
                "empty; it will be disclosed on the methodology page under "
                "#data-gaps if the gap persists."
            ),
        })
        return None

    return prev


def _wait_for_ready(deadline: float) -> None:
    """Block until the instance reports InstanceStatusOk + SystemStatusOk.

    Bounded by whichever comes first: the normal readiness timeout, or
    `deadline` (the invocation's own budget, already reduced by however
    long the start retries took). Overrunning the latter would get us
    killed mid-flight with no alert.
    """
    deadline = min(deadline, time.monotonic() + INSTANCE_READY_TIMEOUT_SECONDS)
    while time.monotonic() < deadline:
        resp = ec2.describe_instance_status(
            InstanceIds=[INSTANCE_ID],
            IncludeAllInstances=True,
        )
        statuses = resp.get("InstanceStatuses", [])
        if statuses:
            s = statuses[0]
            inst_status = s.get("InstanceStatus", {}).get("Status")
            sys_status = s.get("SystemStatus", {}).get("Status")
            state = s.get("InstanceState", {}).get("Name")
            _log.info("status: state=%s instance=%s system=%s",
                      state, inst_status, sys_status)
            if state == "running" and inst_status == "ok" and sys_status == "ok":
                return
        time.sleep(INSTANCE_READY_POLL_INTERVAL)
    raise TimeoutError(
        f"instance {INSTANCE_ID} did not reach InstanceStatusOk within "
        f"{INSTANCE_READY_TIMEOUT_SECONDS}s"
    )


def _deadline_from(context: Any) -> float:
    """Monotonic deadline for this invocation, reserving alert time."""
    getter = getattr(context, "get_remaining_time_in_millis", None)
    remaining = (
        getter() / 1000.0 if callable(getter) else float(LAMBDA_MAX_RUNTIME_SECONDS)
    )
    return time.monotonic() + max(0.0, remaining - HANDLER_RESERVE_SECONDS)


def _stop_if_ours(we_own_lifecycle: bool) -> None:
    """Return the instance to stopped after a failed dispatch."""
    if not we_own_lifecycle:
        return
    try:
        ec2.stop_instances(InstanceIds=[INSTANCE_ID])
    except ClientError as stop_err:
        _log.warning("stop_instances failed: %s", stop_err)


def _send_wrapper(we_own_lifecycle: bool) -> str:
    """Fire-and-forget SendCommand. Returns the SSM command id for the log."""
    # Pass through the env vars the wrapper needs:
    #   WE_OWN_LIFECYCLE — gates the self-stop call at end of run
    #   SNS_TOPIC_ARN    — alert destination
    # /etc/meridian/config.env (placed by the bootstrap script) supplies
    # the rest (SSM secret paths, region). Anything passed here overrides
    # the config-file value because the wrapper sources the file before
    # reading these env vars.
    #
    # shlex.quote on every interpolated value defends against any
    # control-plane mishap that could land shell metacharacters in a
    # Terraform-managed env var or path.
    flag = "1" if we_own_lifecycle else "0"
    # SSM RunCommand runs as root by default. We `sudo -u meridian env`
    # so the wrapper executes as the meridian system user — same UID
    # that owns /data/meridian/repo, the venv, and the log directory.
    # Without this, git refuses to operate (dubious ownership) and any
    # files the wrapper writes end up owned by root, breaking the next
    # run.
    cmd = (
        f"sudo -u meridian env "
        f"WE_OWN_LIFECYCLE={shlex.quote(flag)} "
        f"SNS_TOPIC_ARN={shlex.quote(SNS_TOPIC_ARN)} "
        f"{shlex.quote(WRAPPER_SCRIPT_PATH)}"
    )

    send_resp = ssm.send_command(
        InstanceIds=[INSTANCE_ID],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [cmd]},
        # DELIVERY deadline only: how long SSM keeps trying to hand this
        # command to the agent before marking it DeliveryTimedOut. It has
        # no bearing on how long the wrapper may run, which the wrapper
        # itself owns. See SSM_COMMAND_TIMEOUT_SECONDS above.
        TimeoutSeconds=SSM_COMMAND_TIMEOUT_SECONDS,
        Comment="meridian weekly pipeline (fire-and-forget)",
    )
    cmd_id = send_resp["Command"]["CommandId"]
    _log.info("ssm SendCommand id=%s we_own_lifecycle=%s", cmd_id, flag)
    return cmd_id


def _run(event: dict[str, Any], context: Any) -> dict[str, Any]:
    deadline = _deadline_from(context)
    # Reserve the readiness wait up front so start-retries can't eat the
    # whole invocation and leave a started instance with no dispatch.
    start_deadline = min(
        deadline - INSTANCE_READY_TIMEOUT_SECONDS,
        time.monotonic() + START_RETRY_MAX_SECONDS,
    )

    prev_state = _start_or_defer(start_deadline)
    if prev_state is None:
        return {"status": "deferred", "instance_id": INSTANCE_ID}
    we_own_lifecycle = prev_state == "stopped"

    # Both failure paths below RETURN rather than raise, deliberately:
    # the instance has already been started and stopped again, so an
    # async retry would cold-boot a g5.2xlarge for a fault that is not
    # transient. That also means no retry follows and the on_failure
    # destination never fires, so these two alerts are terminal and are
    # worded that way, and each one files its own run-log record.
    try:
        _wait_for_ready(deadline)
    except TimeoutError as e:
        _record_failed_run(
            _target_week_id(), "instance never reached InstanceStatusOk", e
        )
        _alert("instance failed to become ready (week not sampled)", {
            "instance_id": INSTANCE_ID,
            "previous_state": prev_state,
            "error": str(e),
            "severity": "terminal",
        })
        _stop_if_ours(we_own_lifecycle)
        return {"status": "ready_timeout"}

    try:
        cmd_id = _send_wrapper(we_own_lifecycle=we_own_lifecycle)
    except ClientError as e:
        _record_failed_run(
            _target_week_id(), "SSM SendCommand failed, wrapper never started", e
        )
        _alert("ssm send_command failed (week not sampled)", {
            "instance_id": INSTANCE_ID,
            "error_code": e.response["Error"]["Code"],
            "error": str(e),
            "severity": "terminal",
        })
        _stop_if_ours(we_own_lifecycle)
        return {"status": "send_command_failed"}

    return {
        "status": "dispatched",
        "instance_id": INSTANCE_ID,
        "ssm_command_id": cmd_id,
        "we_own_lifecycle": we_own_lifecycle,
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _log.info("orchestrator start: event=%s", json.dumps(event)[:500])

    # Catch-all: every failure alerts before it propagates. We re-raise
    # rather than swallow so the invocation still counts as failed. That
    # drives the Lambda Errors metric (alarmed in sns.tf), the async
    # retry in lambda.tf, and the on_failure destination once those
    # retries are spent.
    #
    # ALERT SEVERITY, and why it is what it is. This handler cannot know
    # which async attempt it is: Lambda reuses the same request id and
    # the same payload across async retries, so there is no attempt
    # counter to read. It therefore does not get to declare a week lost.
    # On 2026-08-10 it did exactly that at 09:03, telling the operator to
    # disclose a data gap, and the next attempt started the instance at
    # 09:06:28. So the two alerts below are worded as "this attempt
    # failed, a retry is pending" and the authoritative "the week is
    # lost" alert is left to the on_failure SNS destination configured on
    # aws_lambda_function_event_invoke_config in lambda.tf, which by
    # construction publishes only after every retry is exhausted. The
    # in-handler alerts stay because they are the only signal that says
    # WHY, and because they arrive minutes before the destination does.
    try:
        return _run(event, context)
    except CapacityUnavailable as e:
        _record_failed_run(
            _target_week_id(),
            "InsufficientInstanceCapacity: instance did not start",
            e,
        )
        _alert("capacity unavailable on this attempt, retry pending", {
            "instance_id": INSTANCE_ID,
            "error": str(e),
            "severity": "warning",
            "reason": (
                "AWS had no capacity for this instance type in its AZ on this "
                "attempt. Lambda retries this invocation asynchronously twice "
                "more, roughly 1 and then 2 minutes later. Do NOT record a "
                "data gap on the strength of this message. If the retries also "
                "fail, the on_failure destination publishes the terminal "
                "alert, and only then does the no-backfill policy apply and "
                "the week get disclosed under #data-gaps on the methodology "
                "page. A run-log failure record for the target week has been "
                "written to the S3 archive either way."
            ),
        })
        raise
    except Exception as e:
        _record_failed_run(
            _target_week_id(),
            f"orchestrator raised {type(e).__name__}",
            e,
        )
        _alert("orchestrator failed on this attempt, retry pending", {
            "instance_id": INSTANCE_ID,
            "error_type": type(e).__name__,
            "error": str(e),
            "severity": "warning",
            "reason": (
                "Two async retries follow, roughly 1 and then 2 minutes "
                "later. The on_failure destination publishes the terminal "
                "alert if they also fail."
            ),
        })
        raise
