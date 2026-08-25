"""Instance reaper Lambda: the branch that must never fire wrongly.

This function stops an EC2 instance that specter also uses. Everything
here exists to pin the one behaviour that makes that acceptable, which
is that the reaper stops the box ONLY when meridian itself started the
current boot and meridian's own run has already finished. A regression
that made it reap on idleness alone would pass a naive smoke test and
then, some Tuesday, kill somebody's live GPU session.

So the tests below are weighted accordingly: the "does not stop" cases
outnumber the "does stop" case, and each of them asserts on
stop_instances not being called rather than on the returned status
string, because the status is a label and the API call is the damage.

Like the orchestrator, this Lambda ships as a zip and is loaded by path.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest

LAMBDA_SRC = (
    Path(__file__).resolve().parents[2]
    / "infra/terraform/ec2-cohabit/lambda/reaper.py"
)

INSTANCE_ID = "i-0testtesttesttest"

# Captured once at import and used only to build timestamps RELATIVE to
# the real clock, so nothing here has to freeze time. The reaper's only
# time arithmetic is `now - LaunchTime`, and every assertion below is
# about an interval measured in minutes or hours, so the few
# milliseconds of drift between this line and the call under test cannot
# move a result. Patching dt.datetime instead would mean reaching into
# the shared datetime module for the duration of the test.
NOW = dt.datetime.now(dt.UTC)

# Comment on the SSM invocations the orchestrator sends. Spelled out in
# full rather than referencing the module constant so that a change to
# either side shows up here as a failure.
DISPATCH_COMMENT = "meridian weekly pipeline (fire-and-forget)"

IDLE_PROBE_OUTPUT = "GPU_USED_MB=0\nBUSY_PROCS=0\n"
BUSY_PROBE_OUTPUT = "GPU_USED_MB=8100\nBUSY_PROCS=2\n"


def _load_reaper(monkeypatch, env: dict[str, str] | None = None):
    monkeypatch.setenv("INSTANCE_ID", INSTANCE_ID)
    monkeypatch.setenv(
        "SNS_TOPIC_ARN", "arn:aws:sns:us-east-2:000000000000:meridian-pipeline-alerts"
    )
    # Cleared so the defaults under test are genuinely the module's, not
    # whatever happens to be in the developer's shell.
    for name in (
        "DISPATCH_COMMENT_MATCH",
        "MIN_UPTIME_SECONDS",
        "GPU_MEMORY_THRESHOLD_MB",
        "PROBE_TIMEOUT_SECONDS",
        "STOP_WHEN_PROBE_UNAVAILABLE",
        "DRY_RUN",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)

    clients = {
        "ec2": MagicMock(name="ec2"),
        "ssm": MagicMock(name="ssm"),
        "sns": MagicMock(name="sns"),
    }
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: clients[name])

    spec = importlib.util.spec_from_file_location("_lambda_reaper", LAMBDA_SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.ec2, module.ssm, module.sns = clients["ec2"], clients["ssm"], clients["sns"]

    # Defaults describe the shape this function exists for: running box,
    # booted well before the grace period, meridian's run already over,
    # box idle. Individual tests spoil exactly one of those.
    _set_instance(module, state="running", uptime_hours=14)
    _set_invocations(module, [_invocation("TimedOut", NOW - dt.timedelta(hours=13))])
    _set_probe(module, "Success", IDLE_PROBE_OUTPUT)
    return module


def _set_instance(module, *, state: str, uptime_hours: float) -> None:
    module.ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "State": {"Name": state},
                        "LaunchTime": NOW - dt.timedelta(hours=uptime_hours),
                    }
                ]
            }
        ]
    }


def _invocation(status: str, requested: dt.datetime, comment: str = DISPATCH_COMMENT):
    return {"Status": status, "RequestedDateTime": requested, "Comment": comment}


def _set_invocations(module, invocations: list[dict]) -> None:
    module.ssm.list_command_invocations.return_value = {
        "CommandInvocations": invocations
    }


def _set_probe(module, status: str, output: str) -> None:
    module.ssm.send_command.return_value = {"Command": {"CommandId": "probe-1"}}
    module.ssm.get_waiter.return_value = MagicMock()
    module.ssm.get_command_invocation.return_value = {
        "Status": status,
        "StandardOutputContent": output,
    }


@pytest.fixture
def reaper(monkeypatch):
    return _load_reaper(monkeypatch)


@pytest.fixture
def load_reaper(monkeypatch):
    def _load(**env: str):
        return _load_reaper(monkeypatch, env)

    return _load


def _subjects(module) -> list[str]:
    return [c.kwargs["Subject"] for c in module.sns.publish.call_args_list]


# ---------- the one case that stops anything ------------------------


def test_stops_the_instance_meridian_left_running(reaper):
    """2026-W34 exactly: SSM SIGKILLed the wrapper at the one-hour mark,
    the self-stop never ran, and the box idled for hours."""
    result = reaper.lambda_handler({"source": "reaper-schedule"}, None)

    assert result["status"] == "stopped"
    reaper.ec2.stop_instances.assert_called_once_with(InstanceIds=[INSTANCE_ID])
    # A reap is a cleanup after a bug, not a success, and the alert has
    # to say so or the underlying breakage never gets looked at.
    assert any("reaper stopped an instance" in s for s in _subjects(reaper))


# ---------- every case that must not -------------------------------


def test_leaves_a_stopped_instance_alone(reaper):
    _set_instance(reaper, state="stopped", uptime_hours=14)

    result = reaper.lambda_handler({}, None)

    assert result["status"] == "noop"
    reaper.ec2.stop_instances.assert_not_called()


def test_does_not_reap_inside_the_startup_grace_period(reaper):
    """The orchestrator waits up to 600 s for InstanceStatusOk before it
    dispatches, so a freshly started instance legitimately has no
    meridian invocation yet and looks exactly like a specter boot.
    Reaping there would kill the weekly run as it was starting."""
    _set_instance(reaper, state="running", uptime_hours=0.2)
    _set_invocations(reaper, [])

    result = reaper.lambda_handler({}, None)

    assert result["status"] == "noop"
    assert result["reason"] == "within startup grace"
    reaper.ec2.stop_instances.assert_not_called()


def test_never_touches_a_boot_meridian_did_not_start(reaper):
    """The load-bearing test in this file.

    No meridian dispatch since LaunchTime means somebody else started
    the box, which on this shared instance means specter. The reaper has
    no opinion about how idle it looks: it is not meridian's to stop."""
    _set_invocations(reaper, [])

    result = reaper.lambda_handler({}, None)

    assert result["status"] == "noop"
    assert result["reason"] == "not meridian's boot"
    reaper.ec2.stop_instances.assert_not_called()
    # Silence, too. This is the ordinary state of the world six days a
    # week and must not generate mail.
    reaper.sns.publish.assert_not_called()


def test_ignores_dispatches_from_a_previous_boot(reaper):
    """A meridian invocation from last week is not evidence about this
    boot. LaunchTime moves on every start, so the comparison is against
    the current boot rather than the instance's original launch."""
    _set_invocations(
        reaper, [_invocation("Success", NOW - dt.timedelta(days=7))]
    )

    result = reaper.lambda_handler({}, None)

    assert result["reason"] == "not meridian's boot"
    reaper.ec2.stop_instances.assert_not_called()


def test_ignores_ssm_commands_that_are_not_meridians(reaper):
    """Somebody running an unrelated command on the box does not hand
    the instance's lifecycle to meridian."""
    _set_invocations(
        reaper,
        [_invocation("Success", NOW - dt.timedelta(hours=2), comment="specter setup")],
    )

    result = reaper.lambda_handler({}, None)

    assert result["reason"] == "not meridian's boot"
    reaper.ec2.stop_instances.assert_not_called()


@pytest.mark.parametrize("status", ["Pending", "InProgress", "Delayed"])
def test_does_not_stop_a_run_that_is_still_going(reaper, status):
    """The obvious catastrophe: reaping the weekly run mid-sample."""
    _set_invocations(reaper, [_invocation(status, NOW - dt.timedelta(hours=1))])

    result = reaper.lambda_handler({}, None)

    assert result["status"] == "noop"
    reaper.ec2.stop_instances.assert_not_called()


def test_uses_the_newest_dispatch_not_the_first_returned(reaper):
    """list_command_invocations does not promise an order, and an
    unsorted read that happened to land on a stale terminal invocation
    would reap a live run."""
    _set_invocations(
        reaper,
        [
            _invocation("Success", NOW - dt.timedelta(hours=12)),
            _invocation("InProgress", NOW - dt.timedelta(minutes=20)),
        ],
    )

    result = reaper.lambda_handler({}, None)

    assert result["status"] == "noop"
    reaper.ec2.stop_instances.assert_not_called()


def test_does_not_stop_a_busy_box_even_when_meridian_owns_the_boot(reaper):
    """The race the probe exists for: meridian's run ended, then a human
    started GPU work before this function noticed."""
    _set_probe(reaper, "Success", BUSY_PROBE_OUTPUT)

    result = reaper.lambda_handler({}, None)

    assert result["status"] == "noop"
    assert result["reason"] == "busy"
    reaper.ec2.stop_instances.assert_not_called()
    assert any("but it is busy" in s for s in _subjects(reaper))


def test_alerts_rather_than_stopping_when_the_probe_cannot_run(reaper):
    """Refusing to stop an instance whose state cannot be observed. The
    accepted cost is that it keeps billing until a human reads the
    alert; the alternative risks destroying someone's uncommitted work."""
    _set_probe(reaper, "Failed", "")

    result = reaper.lambda_handler({}, None)

    assert result["status"] == "alerted"
    reaper.ec2.stop_instances.assert_not_called()
    assert any("could not verify it is idle" in s for s in _subjects(reaper))


def test_unreadable_gpu_is_not_treated_as_idle(reaper):
    """nvidia-smi failing on a g5 means something is wrong with the box,
    and 'I could not tell' must never collapse into 'it was empty'."""
    _set_probe(reaper, "Success", "GPU_USED_MB=unknown\nBUSY_PROCS=0\n")

    result = reaper.lambda_handler({}, None)

    assert result["status"] == "alerted"
    reaper.ec2.stop_instances.assert_not_called()


def test_unknown_ssm_status_is_treated_as_terminal(reaper):
    """Active statuses are enumerated rather than inferred by exclusion.
    SSM has more terminal states than people remember, and treating an
    unrecognised one as 'still running' would make the reaper fail open
    and silently never reap again."""
    _set_invocations(reaper, [_invocation("Cancelled", NOW - dt.timedelta(hours=13))])

    result = reaper.lambda_handler({}, None)

    assert result["status"] == "stopped"
    reaper.ec2.stop_instances.assert_called_once()


# ---------- knobs ---------------------------------------------------


def test_dry_run_reports_without_stopping(load_reaper):
    module = load_reaper(DRY_RUN="1")

    result = module.lambda_handler({}, None)

    assert result["status"] == "dry_run"
    module.ec2.stop_instances.assert_not_called()


def test_probe_unavailable_can_be_configured_to_stop(load_reaper):
    module = load_reaper(STOP_WHEN_PROBE_UNAVAILABLE="1")
    _set_probe(module, "Failed", "")

    result = module.lambda_handler({}, None)

    assert result["status"] == "stopped"
    module.ec2.stop_instances.assert_called_once()


def test_default_comment_match_recognises_the_orchestrators_dispatch(reaper):
    """Pins the other half of the coupling asserted in
    test_lambda_orchestrator.py. Both sides have to move together."""
    assert reaper.DISPATCH_COMMENT_MATCH in DISPATCH_COMMENT
