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
import json
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

LAMBDA_SRC = (
    Path(__file__).resolve().parents[2]
    / "infra/terraform/ec2-cohabit/lambda/orchestrator.py"
)

INSTANCE_ID = "i-0testtesttesttest"
ARCHIVE_BUCKET = "meridian-archive-test"

def _run_log_field_names() -> tuple[set[str], set[str]]:
    """(all fields, fields with no default) of RunLogEntry.

    The Lambda ships as a standalone zip and cannot import the package,
    so it builds the record by hand. The test can import, and does, so
    the two definitions are checked against each other rather than
    against a copied-out list that would rot. This matters because
    read_run_log() does `RunLogEntry(**obj)`: an extra key raises
    TypeError, a missing non-default key raises TypeError, and the
    record it would choke on is one retention policy keeps forever.
    """
    from dataclasses import MISSING, fields

    from meridian.pipeline.run_log import RunLogEntry

    all_names = {f.name for f in fields(RunLogEntry)}
    required = {
        f.name
        for f in fields(RunLogEntry)
        if f.default is MISSING and f.default_factory is MISSING
    }
    return all_names, required


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


def _load_orchestrator(monkeypatch, env: dict[str, str] | None = None):
    """Import the Lambda by path with stubbed AWS clients and a fake clock.

    Env goes in BEFORE exec_module on purpose: the module reads its
    configuration at import time into module-level constants, so anything
    a test wants to say about env parsing has to be said here. Setting the
    module attribute afterwards tests the attribute, not the parsing.
    """
    monkeypatch.setenv("INSTANCE_ID", INSTANCE_ID)
    monkeypatch.setenv("WRAPPER_SCRIPT_PATH", "/opt/meridian/run-weekly.sh")
    monkeypatch.setenv(
        "SNS_TOPIC_ARN", "arn:aws:sns:us-east-2:000000000000:meridian-pipeline-alerts"
    )
    monkeypatch.setenv("ARCHIVE_BUCKET", ARCHIVE_BUCKET)
    monkeypatch.setenv("ARCHIVE_BUCKET_PREFIX", "meridian/")
    # Not set by default, so the "unset falls back to 600" case is a real
    # observation rather than an accident of the developer's shell.
    monkeypatch.delenv("SSM_COMMAND_TIMEOUT_SECONDS", raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)

    clients = {"ec2": MagicMock(name="ec2"), "ssm": MagicMock(name="ssm"),
               "sns": MagicMock(name="sns"), "s3": MagicMock(name="s3")}
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: clients[name])

    spec = importlib.util.spec_from_file_location("_lambda_orchestrator", LAMBDA_SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "time", _FakeClock())

    # Sensible defaults; individual tests override what they care about.
    clients["ec2"].start_instances.return_value = _started()
    clients["ec2"].describe_instance_status.return_value = _status_ok()
    clients["ssm"].send_command.return_value = {"Command": {"CommandId": "cmd-1"}}

    module.ec2, module.ssm, module.sns, module.s3 = (
        clients["ec2"], clients["ssm"], clients["sns"], clients["s3"]
    )
    return module


@pytest.fixture
def orch(monkeypatch):
    """Load a fresh orchestrator module with stubbed AWS clients."""
    return _load_orchestrator(monkeypatch)


@pytest.fixture
def load_orchestrator(monkeypatch):
    """Same, but the test chooses the environment the module imports with."""
    def _load(**env: str):
        return _load_orchestrator(monkeypatch, env)

    return _load


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


def test_alert_subjects_are_printable_ascii(orch):
    """SNS documents Subject as printable ASCII, rejects anything else
    with an InvalidParameter ClientError, and _alert swallows a rejected
    publish, so a subject carrying an em-dash is an alert that silently
    never arrives and never complains. The deferral notice exercised here
    had exactly that, and it is the most frequently taken alert path in
    the function."""
    orch.ec2.start_instances.return_value = _started(previous="running",
                                                     current="running")

    orch.lambda_handler({"source": "scheduler"}, _Context())

    subjects = _alert_subjects(orch)
    assert subjects
    for subject in subjects:
        assert subject.isascii(), f"SNS will reject this subject: {subject!r}"
        assert subject.isprintable(), f"non-printable in subject: {subject!r}"


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


def test_capacity_retry_spends_almost_all_of_its_budget(orch):
    """The lower bound the upper-bound test structurally cannot catch.

    The old loop compared the NEXT backoff against the remaining budget
    and gave up when the backoff was larger, so it stopped early by
    construction: the 2026-08-10 trace retried at 15/30/60s and declared
    the budget exhausted at 145s of 240s, leaving ~95s of a capacity
    outage unwaited. Every assertion in place at the time passed. A
    ceiling test can never see under-use; only a floor can.
    """
    orch.ec2.start_instances.side_effect = _client_error(
        "InsufficientInstanceCapacity"
    )
    started = orch.time.monotonic()

    with pytest.raises(orch.CapacityUnavailable):
        orch.lambda_handler({"source": "scheduler"}, _Context())

    elapsed = orch.time.monotonic() - started
    assert elapsed >= 0.9 * orch.START_RETRY_MAX_SECONDS, (
        f"only spent {elapsed:.0f}s of a {orch.START_RETRY_MAX_SECONDS}s "
        "capacity-retry budget"
    )


def test_capacity_backoff_is_clamped_not_abandoned(orch):
    """No sleep may overrun the budget, and the last one must fill it.

    Clamping is what makes START_RETRY_MAX_BACKOFF reachable at all: at
    240s of budget the doubling sequence 15/30/60/120 needs ~265s before
    the 120 is ever selected, so under the old compare-and-abort rule it
    was dead code.
    """
    orch.ec2.start_instances.side_effect = _client_error(
        "InsufficientInstanceCapacity"
    )
    sleeps: list[float] = []
    real_sleep = orch.time.sleep

    def _record(seconds: float) -> None:
        sleeps.append(seconds)
        real_sleep(seconds)

    orch.time.sleep = _record

    with pytest.raises(orch.CapacityUnavailable):
        orch.lambda_handler({"source": "scheduler"}, _Context())

    assert sleeps, "capacity errors must be retried at least once"
    assert sum(sleeps) <= orch.START_RETRY_MAX_SECONDS
    # Doubling until the clamp bites, and no sleep beyond the cap.
    assert sleeps[0] == orch.START_RETRY_INITIAL_BACKOFF
    assert max(sleeps) <= orch.START_RETRY_MAX_BACKOFF
    # The tail is short because it was clamped to what was left, not
    # skipped because it did not fit.
    assert sleeps[-1] < orch.START_RETRY_MAX_BACKOFF


def test_recovery_after_capacity_retry_is_announced(orch):
    """A retry that works must say so.

    On 2026-08-10 the operator got "capacity unavailable, instance did
    not start" at 09:03 and nothing at all when the retry started the
    instance at 09:06:28, so the week looked lost for as long as it took
    someone to check the artifacts by hand.
    """
    orch.ec2.start_instances.side_effect = [
        _client_error("InsufficientInstanceCapacity"),
        _started(),
    ]

    result = orch.lambda_handler({"source": "scheduler"}, _Context())

    assert result["status"] == "dispatched"
    assert any("recovered on retry" in s for s in _alert_subjects(orch))


def test_capacity_alert_does_not_declare_the_week_lost(orch):
    """The handler cannot know whether an async retry follows, so it is
    not allowed to tell the operator to disclose a data gap. That call
    belongs to the on_failure destination in lambda.tf, which fires only
    once the retries are spent."""
    orch.ec2.start_instances.side_effect = _client_error(
        "InsufficientInstanceCapacity"
    )

    with pytest.raises(orch.CapacityUnavailable):
        orch.lambda_handler({"source": "scheduler"}, _Context())

    call = orch.sns.publish.call_args_list[0]
    assert "retry pending" in call.kwargs["Subject"]
    body = json.loads(call.kwargs["Message"])
    assert body["severity"] == "warning"
    assert "Do NOT record a data gap" in body["reason"]


# ---------- a lost week records itself -------------------------------


def _put_object_calls(module) -> list:
    return module.s3.put_object.call_args_list


def _failure_record(module) -> dict:
    calls = _put_object_calls(module)
    assert len(calls) == 1, f"expected exactly one failure record, got {len(calls)}"
    return json.loads(calls[0].kwargs["Body"].decode("utf-8"))


def test_capacity_failure_writes_a_run_log_record(orch):
    """2026-W30 and 2026-W31 left no line in the append-only run log, so
    the outage is prose on the methodology page and nothing else. A week
    that dies before the pipeline starts has to record itself."""
    orch.ec2.start_instances.side_effect = _client_error(
        "InsufficientInstanceCapacity"
    )

    with pytest.raises(orch.CapacityUnavailable):
        orch.lambda_handler({"source": "scheduler"}, _Context())

    call = _put_object_calls(orch)[0]
    assert call.kwargs["Bucket"] == ARCHIVE_BUCKET
    week = orch._target_week_id()
    assert call.kwargs["Key"] == f"meridian/run_log/failures/{week}.json"

    entry = _failure_record(orch)
    assert entry["week_id"] == week
    assert entry["pairs_complete"] == 0
    assert entry["total_samples_written"] == 0
    assert "InsufficientInstanceCapacity" in entry["note"]
    assert entry["errors"][0]["error_type"] == "CapacityUnavailable"


def test_failure_record_matches_the_run_log_entry_shape(orch):
    """The record must survive read_run_log(), which does
    `RunLogEntry(**obj)` and raises on any unexpected key."""
    orch.ec2.start_instances.side_effect = _client_error(
        "InsufficientInstanceCapacity"
    )

    with pytest.raises(orch.CapacityUnavailable):
        orch.lambda_handler({"source": "scheduler"}, _Context())

    entry = _failure_record(orch)
    all_fields, required = _run_log_field_names()
    assert set(entry) <= all_fields, (
        f"unknown key(s) {sorted(set(entry) - all_fields)} would make "
        "read_run_log() raise TypeError"
    )
    assert required <= set(entry), (
        f"missing required field(s) {sorted(required - set(entry))}"
    )

    from meridian.pipeline.run_log import RunLogEntry

    RunLogEntry(**entry)  # the actual reader path, not a proxy for it


def test_failure_record_targets_the_week_the_run_would_have_sampled(orch):
    """The Monday run samples the week that just ended, so a failure on
    Monday 2026-08-17 has to be filed against 2026-W33, not W34. Same
    rule as `date -u --date=yesterday` in scripts/run-weekly.sh."""
    from datetime import datetime, timezone

    monday = datetime(2026, 8, 17, 9, 3, tzinfo=timezone.utc)
    assert orch._target_week_id(monday) == "2026-W33"
    # And a Monday that lands in the first days of January still names
    # the outgoing ISO year.
    assert orch._target_week_id(
        datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    ) == "2026-W01"


def test_unexpected_failure_also_records_the_week(orch):
    orch.ec2.start_instances.side_effect = _client_error("UnauthorizedOperation")

    with pytest.raises(ClientError):
        orch.lambda_handler({"source": "scheduler"}, _Context())

    entry = _failure_record(orch)
    assert entry["pairs_complete"] == 0
    assert "ClientError" in entry["note"]


def test_ready_timeout_records_the_week(orch):
    """This path returns instead of raising, so no async retry and no
    on_failure destination follows it. Nothing else will file the record."""
    orch.ec2.describe_instance_status.return_value = {"InstanceStatuses": []}

    orch.lambda_handler({"source": "scheduler"}, _Context())

    entry = _failure_record(orch)
    assert "InstanceStatusOk" in entry["note"]


def test_run_log_record_is_skipped_when_no_bucket_is_configured(orch, monkeypatch):
    """A half-configured deployment must still fail loudly on SNS rather
    than blow up inside the bookkeeping that describes the failure."""
    monkeypatch.setattr(orch, "ARCHIVE_BUCKET", "")
    orch.ec2.start_instances.side_effect = _client_error(
        "InsufficientInstanceCapacity"
    )

    with pytest.raises(orch.CapacityUnavailable):
        orch.lambda_handler({"source": "scheduler"}, _Context())

    orch.s3.put_object.assert_not_called()
    assert any("capacity unavailable" in s for s in _alert_subjects(orch))


def test_s3_failure_does_not_mask_the_original_error(orch):
    """Bookkeeping is never allowed to become the reported fault."""
    orch.s3.put_object.side_effect = _client_error("AccessDenied", "PutObject")
    orch.ec2.start_instances.side_effect = _client_error(
        "InsufficientInstanceCapacity"
    )

    with pytest.raises(orch.CapacityUnavailable):
        orch.lambda_handler({"source": "scheduler"}, _Context())

    assert any("capacity unavailable" in s for s in _alert_subjects(orch))


def test_non_clienterror_from_s3_still_alerts(orch):
    """The regression the AccessDenied test above structurally cannot see.

    _record_failed_run guarded only ClientError, and it runs BEFORE
    _alert on every terminal path, so any other botocore exception took
    the alert with it: EndpointConnectionError, ConnectTimeoutError,
    ReadTimeoutError, NoCredentialsError and ParamValidationError are
    none of them ClientError. A capacity exhaustion with S3 unreachable
    published zero SNS messages, which is the 2026-W30/W31 silence
    reintroduced by the bookkeeping written to prevent it.

    The original fault must still be what propagates, and the alert must
    still go out.
    """
    orch.s3.put_object.side_effect = EndpointConnectionError(
        endpoint_url="https://s3.us-east-2.amazonaws.com"
    )
    orch.ec2.start_instances.side_effect = _client_error(
        "InsufficientInstanceCapacity"
    )

    with pytest.raises(orch.CapacityUnavailable):
        orch.lambda_handler({"source": "scheduler"}, _Context())

    assert orch.sns.publish.called, "an unreachable S3 swallowed the alert"
    assert any("capacity unavailable" in s for s in _alert_subjects(orch))


def test_non_clienterror_from_s3_still_stops_the_instance(orch):
    """Worse on the readiness-timeout path: it stops the instance itself
    rather than raising, so an escaping bookkeeping error skipped both the
    alert and the stop, leaving a g5.2xlarge running at roughly $1/hour
    with nothing said."""
    orch.s3.put_object.side_effect = EndpointConnectionError(
        endpoint_url="https://s3.us-east-2.amazonaws.com"
    )
    orch.ec2.describe_instance_status.return_value = {"InstanceStatuses": []}

    result = orch.lambda_handler({"source": "scheduler"}, _Context())

    assert result["status"] == "ready_timeout"
    assert any("failed to become ready" in s for s in _alert_subjects(orch))
    orch.ec2.stop_instances.assert_called_once()


# ---------- SSM delivery deadline -----------------------------------


def test_ssm_command_timeout_is_read_from_the_environment(load_orchestrator):
    """The env var was set by Terraform and read by nobody until 2026-08:
    _send_wrapper hardcoded 600, so raising the variable applied cleanly
    and changed nothing.

    The environment has to be set before the module is imported for this
    to mean anything. An earlier version of this test never touched the
    environment at all: it asserted 600 with the variable unset, which the
    old hardcoded 600 also satisfied, and then assigned the module
    attribute directly, which proves only that _send_wrapper reads a
    module global. Both halves passed against the code they existed to
    reject.
    """
    module = load_orchestrator(SSM_COMMAND_TIMEOUT_SECONDS="1234")

    assert module.SSM_COMMAND_TIMEOUT_SECONDS == 1234
    module.lambda_handler({"source": "scheduler"}, _Context())
    assert module.ssm.send_command.call_args.kwargs["TimeoutSeconds"] == 1234


def test_ssm_command_timeout_defaults_when_unset(load_orchestrator):
    """No variable at all keeps the value the call was hardcoded to, so
    wiring the knob up changed nothing on its own."""
    module = load_orchestrator()

    assert module.SSM_COMMAND_TIMEOUT_SECONDS == 600
    module.lambda_handler({"source": "scheduler"}, _Context())
    assert module.ssm.send_command.call_args.kwargs["TimeoutSeconds"] == 600


def test_ssm_command_timeout_falls_back_when_the_variable_is_empty(load_orchestrator):
    """Terraform renders this with tostring(var.ssm_command_timeout_seconds),
    and an unset or blanked tfvar renders an empty string rather than
    removing the variable. int("") raises ValueError at import, which in a
    Lambda is an init failure with no handler code running to explain it,
    so the `or "600"` in the parse is load-bearing."""
    module = load_orchestrator(SSM_COMMAND_TIMEOUT_SECONDS="")

    assert module.SSM_COMMAND_TIMEOUT_SECONDS == 600
    module.lambda_handler({"source": "scheduler"}, _Context())
    assert module.ssm.send_command.call_args.kwargs["TimeoutSeconds"] == 600


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
