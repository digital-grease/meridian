"""Weekly-pipeline orchestrator Lambda: dispatch, deferral, and — the
reason this file exists — that no failure path exits silently.

2026-W30 and 2026-W31 were both lost to an uncaught
InsufficientInstanceCapacity from StartInstances. No SNS alert fired,
so the gap went unnoticed for two weeks. The alert assertions below
are regression tests for that, not incidental checks.

The Lambda lives outside the package (it ships as a zip built by
Terraform), so it is loaded by path rather than imported.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError

LAMBDA_SRC = (
    Path(__file__).resolve().parents[2]
    / "infra/terraform/ec2-cohabit/lambda/orchestrator.py"
)

INSTANCE_ID = "i-0testtesttesttest"


class _FakeClock:
    """Stands in for the `time` module inside the orchestrator.

    Substituted for the module's own `time` reference rather than
    patching the real module, so nothing global is disturbed. sleep()
    advances the clock, which is what lets the retry-budget tests
    terminate instead of spinning.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Context:
    """Minimal stand-in for the Lambda context object."""

    def __init__(self, remaining_seconds: float = 900.0) -> None:
        self._remaining = remaining_seconds

    def get_remaining_time_in_millis(self) -> float:
        return self._remaining * 1000.0


def _client_error(code: str, op: str = "StartInstances") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


def _started(previous: str = "stopped", current: str = "pending") -> dict:
    return {
        "StartingInstances": [
            {"PreviousState": {"Name": previous}, "CurrentState": {"Name": current}}
        ]
    }


def _status_ok() -> dict:
    return {
        "InstanceStatuses": [
            {
                "InstanceState": {"Name": "running"},
                "InstanceStatus": {"Status": "ok"},
                "SystemStatus": {"Status": "ok"},
            }
        ]
    }


@pytest.fixture
def orch(monkeypatch):
    """Load a fresh orchestrator module with stubbed AWS clients."""
    monkeypatch.setenv("INSTANCE_ID", INSTANCE_ID)
    monkeypatch.setenv("WRAPPER_SCRIPT_PATH", "/opt/meridian/run-weekly.sh")
    monkeypatch.setenv(
        "SNS_TOPIC_ARN", "arn:aws:sns:us-east-2:000000000000:meridian-pipeline-alerts"
    )

    clients = {"ec2": MagicMock(name="ec2"), "ssm": MagicMock(name="ssm"),
               "sns": MagicMock(name="sns")}
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: clients[name])

    spec = importlib.util.spec_from_file_location("_lambda_orchestrator", LAMBDA_SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "time", _FakeClock())

    # Sensible defaults; individual tests override what they care about.
    clients["ec2"].start_instances.return_value = _started()
    clients["ec2"].describe_instance_status.return_value = _status_ok()
    clients["ssm"].send_command.return_value = {"Command": {"CommandId": "cmd-1"}}

    module.ec2, module.ssm, module.sns = clients["ec2"], clients["ssm"], clients["sns"]
    return module


def _alert_subjects(module) -> list[str]:
    return [c.kwargs["Subject"] for c in module.sns.publish.call_args_list]


# ---------- happy path ----------------------------------------------


def test_dispatches_wrapper_when_instance_was_stopped(orch):
    result = orch.lambda_handler({"source": "scheduler"}, _Context())

    assert result["status"] == "dispatched"
    assert result["we_own_lifecycle"] is True
    assert result["ssm_command_id"] == "cmd-1"
    orch.ssm.send_command.assert_called_once()


def test_defers_without_dispatching_when_instance_already_running(orch):
    orch.ec2.start_instances.return_value = _started(previous="running",
                                                     current="running")

    result = orch.lambda_handler({"source": "scheduler"}, _Context())

    assert result["status"] == "deferred"
    orch.ssm.send_command.assert_not_called()
    assert any("deferred" in s for s in _alert_subjects(orch))


# ---------- capacity handling ---------------------------------------


def test_retries_capacity_error_then_succeeds(orch):
    orch.ec2.start_instances.side_effect = [
        _client_error("InsufficientInstanceCapacity"),
        _client_error("InsufficientInstanceCapacity"),
        _started(),
    ]

    result = orch.lambda_handler({"source": "scheduler"}, _Context())

    assert result["status"] == "dispatched"
    assert orch.ec2.start_instances.call_count == 3
    orch.ssm.send_command.assert_called_once()


def test_capacity_exhaustion_alerts_and_raises(orch):
    """The 2026-W30/W31 regression: this must never fail quietly."""
    orch.ec2.start_instances.side_effect = _client_error(
        "InsufficientInstanceCapacity"
    )

    with pytest.raises(orch.CapacityUnavailable):
        orch.lambda_handler({"source": "scheduler"}, _Context())

    subjects = _alert_subjects(orch)
    assert len(subjects) == 1
    assert "capacity unavailable" in subjects[0]
    orch.ssm.send_command.assert_not_called()


def test_capacity_retry_stays_within_its_budget(orch):
    """Start retries must not eat the readiness wait's reserved time."""
    orch.ec2.start_instances.side_effect = _client_error(
        "InsufficientInstanceCapacity"
    )
    started = orch.time.monotonic()

    with pytest.raises(orch.CapacityUnavailable):
        orch.lambda_handler({"source": "scheduler"}, _Context())

    elapsed = orch.time.monotonic() - started
    assert elapsed <= orch.START_RETRY_MAX_SECONDS


# ---------- everything else still alerts ----------------------------


def test_non_retryable_start_error_is_not_retried_but_alerts(orch):
    orch.ec2.start_instances.side_effect = _client_error("UnauthorizedOperation")

    with pytest.raises(ClientError):
        orch.lambda_handler({"source": "scheduler"}, _Context())

    assert orch.ec2.start_instances.call_count == 1
    assert any("orchestrator failed" in s for s in _alert_subjects(orch))


def test_readiness_timeout_alerts_and_stops_instance(orch):
    orch.ec2.describe_instance_status.return_value = {"InstanceStatuses": []}

    result = orch.lambda_handler({"source": "scheduler"}, _Context())

    assert result["status"] == "ready_timeout"
    assert any("failed to become ready" in s for s in _alert_subjects(orch))
    orch.ec2.stop_instances.assert_called_once()


def test_send_command_failure_alerts_and_stops_instance(orch):
    orch.ssm.send_command.side_effect = _client_error("AccessDenied", "SendCommand")

    result = orch.lambda_handler({"source": "scheduler"}, _Context())

    assert result["status"] == "send_command_failed"
    assert any("send_command failed" in s for s in _alert_subjects(orch))
    orch.ec2.stop_instances.assert_called_once()


def test_readiness_wait_respects_a_short_invocation_deadline(orch):
    """A near-exhausted invocation must give up, not get killed mid-wait."""
    orch.ec2.describe_instance_status.return_value = {"InstanceStatuses": []}
    started = orch.time.monotonic()

    result = orch.lambda_handler({"source": "scheduler"}, _Context(remaining_seconds=90))

    assert result["status"] == "ready_timeout"
    assert orch.time.monotonic() - started < orch.INSTANCE_READY_TIMEOUT_SECONDS
