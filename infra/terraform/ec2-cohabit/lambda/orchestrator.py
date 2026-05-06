"""Meridian weekly-pipeline orchestrator (AWS Lambda).

Triggered by EventBridge Scheduler (Mon 04:00 America/Chicago by
default). Cohabits specter's g5.2xlarge.

Architecture is fire-and-forget by necessity — Lambda's 15-minute hard
timeout cannot wait out a 30–90 minute pipeline run. So this Lambda's
only job is:

  1. Try to start the instance.
  2. If it was already running, the cohabit policy says we defer (no
     backfill) — publish a 'deferred' alert and exit.
  3. Otherwise wait for the instance to become SSM-reachable.
  4. Send the wrapper script via SSM and exit immediately.

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


def _start_or_defer() -> str | None:
    """Return previous state, or None to signal 'defer — instance was running'."""
    resp = ec2.start_instances(InstanceIds=[INSTANCE_ID])
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


def _wait_for_ready() -> None:
    """Block until the instance reports InstanceStatusOk + SystemStatusOk."""
    deadline = time.monotonic() + INSTANCE_READY_TIMEOUT_SECONDS
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


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _log.info("orchestrator start: event=%s", json.dumps(event)[:500])

    prev_state = _start_or_defer()
    if prev_state is None:
        return {"status": "deferred", "instance_id": INSTANCE_ID}
    we_own_lifecycle = prev_state == "stopped"

    try:
        _wait_for_ready()
    except TimeoutError as e:
        _alert("instance failed to become ready", {
            "instance_id": INSTANCE_ID,
            "previous_state": prev_state,
            "error": str(e),
        })
        if we_own_lifecycle:
            try:
                ec2.stop_instances(InstanceIds=[INSTANCE_ID])
            except ClientError as stop_err:
                _log.warning("stop_instances failed: %s", stop_err)
        return {"status": "ready_timeout"}

    try:
        cmd_id = _send_wrapper(we_own_lifecycle=we_own_lifecycle)
    except ClientError as e:
        _alert("ssm send_command failed", {
            "instance_id": INSTANCE_ID,
            "error_code": e.response["Error"]["Code"],
            "error": str(e),
        })
        if we_own_lifecycle:
            try:
                ec2.stop_instances(InstanceIds=[INSTANCE_ID])
            except ClientError as stop_err:
                _log.warning("stop_instances failed: %s", stop_err)
        return {"status": "send_command_failed"}

    return {
        "status": "dispatched",
        "instance_id": INSTANCE_ID,
        "ssm_command_id": cmd_id,
        "we_own_lifecycle": we_own_lifecycle,
    }
