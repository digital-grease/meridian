"""Dead man's switch Lambda: does it page when nothing ran?

One behaviour is load-bearing: an empty metric window must publish, and
a publish that fails must escalate rather than go quiet.

Note what this function is NOT for, since the tempting example is the
wrong one. It would not have caught 2026-W30 or 2026-W31: those weeks
were invoked normally and then raised, and AWS/Lambda Invocations counts
a failed invocation the same as a successful one (both Mondays still
read Sum = 1.0). The Errors alarm covers that shape. This covers the
disjoint case where nothing is invoked at all, which nothing else in the
account can see.

The subtle part is what "empty" looks like on the wire. CloudWatch's
GetMetricStatistics omits periods with no data rather than returning
them as zero, so the total absence of a run arrives as an empty
Datapoints list, not as a datapoint whose Sum is 0. A handler that
iterates datapoints looking for a zero would therefore find nothing to
look at and report healthy, which is the failure this file exists to
prevent.

The Lambda ships as a Terraform-built zip outside the package, so it is
loaded by path rather than imported, matching test_lambda_orchestrator.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

LAMBDA_SRC = (
    Path(__file__).resolve().parents[2]
    / "infra/terraform/ec2-cohabit/lambda/canary.py"
)

TOPIC = "arn:aws:sns:us-east-2:000000000000:meridian-pipeline-alerts"
WATCHED = "meridian-orchestrator"


def _load_canary(monkeypatch):
    """Import the Lambda by path with stubbed AWS clients.

    Env is set before exec_module because the module reads it at import
    time into module-level constants, same as the orchestrator.
    """
    monkeypatch.setenv("WATCHED_FUNCTION_NAME", WATCHED)
    monkeypatch.setenv("SNS_TOPIC_ARN", TOPIC)

    clients = {
        "cloudwatch": MagicMock(name="cloudwatch"),
        "sns": MagicMock(name="sns"),
    }
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: clients[name])

    spec = importlib.util.spec_from_file_location("_lambda_canary", LAMBDA_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, clients


def _datapoints(*sums: float) -> dict:
    return {"Datapoints": [{"Sum": s} for s in sums]}


def test_empty_window_publishes_the_alert(monkeypatch):
    """The 2026-W30 shape: nothing ran, so CloudWatch returns nothing.

    This is the whole point of the function. An empty Datapoints list is
    the wire representation of "never invoked", not an error.
    """
    mod, clients = _load_canary(monkeypatch)
    clients["cloudwatch"].get_metric_statistics.return_value = {"Datapoints": []}

    result = mod.lambda_handler({}, None)

    assert result["ok"] is False
    assert result["invocations"] == 0
    clients["sns"].publish.assert_called_once()
    kwargs = clients["sns"].publish.call_args.kwargs
    assert kwargs["TopicArn"] == TOPIC
    assert "NO WEEKLY RUN" in kwargs["Subject"]
    # The operator needs the runbook and the two diagnostic commands, not
    # just the fact of the miss.
    assert "ec2-runbook.md" in kwargs["Message"]
    assert "get-schedule" in kwargs["Message"]


def test_missing_datapoints_key_is_treated_as_empty(monkeypatch):
    """A response with no Datapoints key at all must not raise.

    Same verdict as the empty list. A KeyError here would surface as a
    function error and the miss would go unreported, trading a silent
    gap for a noisy one that still tells nobody a run was lost.
    """
    mod, clients = _load_canary(monkeypatch)
    clients["cloudwatch"].get_metric_statistics.return_value = {}

    result = mod.lambda_handler({}, None)

    assert result["ok"] is False
    clients["sns"].publish.assert_called_once()


def test_explicit_zero_datapoints_also_publish(monkeypatch):
    """Belt and braces: a datapoint whose Sum is 0 counts as no run.

    CloudWatch is not expected to emit this, but the verdict must come
    from the total rather than from whether the list was empty.
    """
    mod, clients = _load_canary(monkeypatch)
    clients["cloudwatch"].get_metric_statistics.return_value = _datapoints(0.0, 0.0)

    result = mod.lambda_handler({}, None)

    assert result["ok"] is False
    clients["sns"].publish.assert_called_once()


def test_a_run_in_the_window_stays_quiet(monkeypatch):
    """The healthy case must not page. A canary that cries every week
    gets muted, and then it is worse than not having one."""
    mod, clients = _load_canary(monkeypatch)
    clients["cloudwatch"].get_metric_statistics.return_value = _datapoints(1.0)

    result = mod.lambda_handler({}, None)

    assert result["ok"] is True
    assert result["invocations"] == 1
    clients["sns"].publish.assert_not_called()


def test_retried_invocations_sum_rather_than_double_report(monkeypatch):
    """2026-W32 recorded several invocations across the async retries.

    Multiple datapoints are normal, not suspicious: the verdict is on
    the sum being non-zero, so a week that needed retries reads healthy.
    """
    mod, clients = _load_canary(monkeypatch)
    clients["cloudwatch"].get_metric_statistics.return_value = _datapoints(2.0, 1.0)

    result = mod.lambda_handler({}, None)

    assert result["ok"] is True
    assert result["invocations"] == 3
    clients["sns"].publish.assert_not_called()


def test_queries_the_orchestrator_over_the_right_window(monkeypatch):
    mod, clients = _load_canary(monkeypatch)
    clients["cloudwatch"].get_metric_statistics.return_value = _datapoints(1.0)

    mod.lambda_handler({}, None)

    kwargs = clients["cloudwatch"].get_metric_statistics.call_args.kwargs
    assert kwargs["Namespace"] == "AWS/Lambda"
    assert kwargs["MetricName"] == "Invocations"
    assert kwargs["Dimensions"] == [{"Name": "FunctionName", "Value": WATCHED}]
    assert kwargs["Statistics"] == ["Sum"]

    span = kwargs["EndTime"] - kwargs["StartTime"]
    assert span == dt.timedelta(hours=36)
    # Both bounds must be tz-aware, or the comparison against
    # CloudWatch's UTC timestamps is ambiguous.
    assert kwargs["StartTime"].tzinfo is not None
    assert kwargs["EndTime"].tzinfo is not None
    # The window must divide into at most 1440 datapoints, the API cap.
    assert span.total_seconds() / kwargs["Period"] <= 1440


def test_window_cannot_reach_a_previous_weeks_run(monkeypatch):
    """The false negative this window is sized to make impossible.

    A dead man's switch may cry wolf; it may never stay quiet through a
    real miss. The trailing-8-days form originally specified for this
    function reaches back to the previous Monday at 12:00 UTC, so a week
    that ran three or more hours late still contributes a datapoint
    seven days on and reports the *next* week healthy when nothing ran.

    Anchored on the firing time, the window must cover every hour of the
    Monday being checked and no part of any other Monday, however late
    that one started.
    """
    fire = dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.timezone.utc)  # a Tuesday
    mod, _ = _load_canary(monkeypatch)
    start = fire - mod.LOOKBACK

    this_monday_00 = dt.datetime(2026, 8, 17, 0, 0, tzinfo=dt.timezone.utc)
    this_monday_late = dt.datetime(2026, 8, 17, 23, 0, tzinfo=dt.timezone.utc)
    prev_monday_late = dt.datetime(2026, 8, 10, 12, 30, tzinfo=dt.timezone.utc)

    # The whole of the Monday under test is in scope, including a run
    # that limped in near midnight.
    assert start <= this_monday_00 <= fire
    assert start <= this_monday_late <= fire
    # The previous week cannot contribute, even 3.5 hours late.
    assert not (start <= prev_monday_late <= fire)


def test_sns_denial_escalates_instead_of_going_quiet(monkeypatch):
    """The silent-failure path this function must not have.

    A run was missed, the canary detected it, and the publish was
    denied. Swallowing that leaves AWS/Lambda Errors at 0 and
    meridian-canary-errors silent, so the detection reaches nobody: a
    log line is not a metric and there is no metric filter on this log
    group. Raising is what turns the failed publish into an Errors
    datapoint, which pages through CloudWatch's own path to the topic
    rather than through the grant that just failed.

    AccessDenied is the realistic case here, which is why it is the one
    tested: see the topic-policy warning in sns.tf.
    """
    mod, clients = _load_canary(monkeypatch)
    clients["cloudwatch"].get_metric_statistics.return_value = {"Datapoints": []}
    clients["sns"].publish.side_effect = ClientError(
        {"Error": {"Code": "AuthorizationError", "Message": "denied"}}, "Publish"
    )

    with pytest.raises(ClientError):
        mod.lambda_handler({}, None)


def test_non_clienterror_from_sns_also_escalates(monkeypatch):
    """The guard must be on Exception, not ClientError.

    The orchestrator shipped exactly this bug and it was caught in
    review two days earlier: a narrow `except ClientError` let a
    connection-level failure skip the alert entirely. Connection and
    credential errors are not ClientError, so a canary that only caught
    ClientError would still be silent on those.
    """
    mod, clients = _load_canary(monkeypatch)
    clients["cloudwatch"].get_metric_statistics.return_value = {"Datapoints": []}
    clients["sns"].publish.side_effect = EndpointConnectionError(
        endpoint_url="https://sns.us-east-2.amazonaws.com"
    )

    with pytest.raises(EndpointConnectionError):
        mod.lambda_handler({}, None)


def test_alert_subject_fits_the_sns_limit(monkeypatch):
    """SNS rejects a Subject over 100 characters, which would turn a
    reported miss into an unreported one."""
    mod, clients = _load_canary(monkeypatch)
    clients["cloudwatch"].get_metric_statistics.return_value = {"Datapoints": []}

    mod.lambda_handler({}, None)

    subject = clients["sns"].publish.call_args.kwargs["Subject"]
    assert 0 < len(subject) <= 100


def test_missing_required_env_fails_at_import(monkeypatch):
    """Configuration is read at import time, so a missing variable must
    fail loudly on cold start rather than at the moment it is needed."""
    monkeypatch.delenv("WATCHED_FUNCTION_NAME", raising=False)
    monkeypatch.setenv("SNS_TOPIC_ARN", TOPIC)
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: MagicMock())

    spec = importlib.util.spec_from_file_location("_lambda_canary_noenv", LAMBDA_SRC)
    mod = importlib.util.module_from_spec(spec)
    with pytest.raises(KeyError):
        spec.loader.exec_module(mod)
