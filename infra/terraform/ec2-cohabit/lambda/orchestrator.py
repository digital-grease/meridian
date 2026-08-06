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

Every exit path alerts. 2026-W30 and 2026-W31 were lost because
`ec2.start_instances` raised InsufficientInstanceCapacity straight out
of the handler: the run never happened, SNS said nothing, and the only
signal was the publish workflow 404ing six hours later. Two weeks went
by before anyone noticed. Nothing here may fail quietly again.

The wrapper script (deployed in Phase 3 at the path in
WRAPPER_SCRIPT_PATH) takes over from there: pre-flight contention
check, secret fetch from SSM Parameter Store, pipeline run, SNS alert
on outcome, and `ec2:StopInstances` on itself if WE_OWN_LIFECYCLE=1.

Environment variables (set by the Terraform module):
  INSTANCE_ID         — specter's EC2 instance id
  WRAPPER_SCRIPT_PATH — absolute path of the wrapper on the instance
  SNS_TOPIC_ARN       — for deferral / orchestrator-side failure alerts
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

_log = logging.getLogger()
_log.setLevel(logging.INFO)

INSTANCE_ID = os.environ["INSTANCE_ID"]
WRAPPER_SCRIPT_PATH = os.environ["WRAPPER_SCRIPT_PATH"]
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]

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
# readiness wait. Riding out a longer outage is the EventBridge
# Scheduler's job (see retry_policy in scheduler.tf), which re-invokes
# us with fresh budget over a multi-hour window.
START_RETRY_MAX_SECONDS = 240
START_RETRY_INITIAL_BACKOFF = 15
START_RETRY_MAX_BACKOFF = 120

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


def _alert(subject: str, body: dict[str, Any]) -> None:
    """Best-effort SNS publish. Swallow publish errors — CloudWatch logs
    are the primary signal; SNS is a notification convenience."""
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"[meridian] {subject}"[:100],
            Message=json.dumps(body, indent=2, default=str),
        )
    except ClientError as e:
        _log.warning("SNS publish failed: %s", e)


def _start_or_defer(deadline: float) -> str | None:
    """Return previous state, or None to signal 'defer — instance was running'.

    Retries capacity errors with exponential backoff until `deadline`
    (a time.monotonic() value). Raises CapacityUnavailable if the
    budget runs out with capacity still unavailable; any other
    ClientError propagates untouched, since retrying a permissions or
    malformed-request error just wastes the budget.
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
            if remaining <= backoff:
                _log.error(
                    "capacity unavailable after %d attempt(s); budget exhausted",
                    attempt,
                )
                raise CapacityUnavailable(
                    f"{code} starting {INSTANCE_ID} after {attempt} attempt(s)"
                ) from e
            _log.warning(
                "attempt %d: %s — retrying in %ds (%.0fs budget left)",
                attempt, code, backoff, remaining,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, START_RETRY_MAX_BACKOFF)

    state_change = resp["StartingInstances"][0]
    prev = state_change["PreviousState"]["Name"]
    cur = state_change["CurrentState"]["Name"]
    _log.info("ec2.start_instances: previous=%s current=%s", prev, cur)

    if prev == "running":
        _alert("deferred — instance already running", {
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
        # SSM enforces an upper bound on its own queue/timeout; the
        # wrapper itself owns the wall-clock budget.
        TimeoutSeconds=600,
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

    try:
        _wait_for_ready(deadline)
    except TimeoutError as e:
        _alert("instance failed to become ready", {
            "instance_id": INSTANCE_ID,
            "previous_state": prev_state,
            "error": str(e),
        })
        _stop_if_ours(we_own_lifecycle)
        return {"status": "ready_timeout"}

    try:
        cmd_id = _send_wrapper(we_own_lifecycle=we_own_lifecycle)
    except ClientError as e:
        _alert("ssm send_command failed", {
            "instance_id": INSTANCE_ID,
            "error_code": e.response["Error"]["Code"],
            "error": str(e),
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
    # rather than swallow so the invocation still counts as failed —
    # that drives the Lambda Errors metric and, more importantly, the
    # EventBridge Scheduler retry that gives a capacity outage another
    # attempt later in the morning.
    try:
        return _run(event, context)
    except CapacityUnavailable as e:
        _alert("capacity unavailable — instance did not start", {
            "instance_id": INSTANCE_ID,
            "error": str(e),
            "reason": (
                "AWS had no capacity for this instance type in its AZ. The "
                "Scheduler will retry within its event-age window. If every "
                "retry fails the week is lost: per the no-backfill policy it "
                "must be disclosed on the methodology page under #data-gaps "
                "rather than sampled late."
            ),
        })
        raise
    except Exception as e:
        _alert("orchestrator failed", {
            "instance_id": INSTANCE_ID,
            "error_type": type(e).__name__,
            "error": str(e),
        })
        raise
