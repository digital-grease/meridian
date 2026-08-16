"""Dead man's switch: page when the weekly run did not happen at all.

Every other check in this account watches the pipeline for a signal it
emits. This one watches for the absence of one, which is a different and
strictly harder problem: a Monday that never starts emits no error, no
alert, and no metric datapoint to alarm on. There is nothing to observe
except a hole.

Why this is a Lambda and not a CloudWatch alarm
-----------------------------------------------
It was written as a metric alarm first: AWS/Lambda Invocations, period
86400, evaluation_periods 7, treat_missing_data "breaching", i.e. "the
trailing seven one-day windows all contained zero invocations". The
logic is right and the resource is not applyable. PutMetricAlarm
enforces an API-side rule terraform validate cannot see: an alarm's
total evaluation period cannot exceed one day, so period x
evaluation_periods must be <= 86400. Seven days of one-day periods is
604800 and the API rejects it with a ValidationError, taking the whole
apply down with it.

No variation escapes that rule. A weekly job cannot be watched by an
alarm whose entire memory is 24 hours, because 24 hours of silence is
the normal state six days out of seven. A scheduled function has no such
constraint: it can look back as far as it likes, because it is not an
alarm.

What this does and does not catch
---------------------------------
It catches the orchestrator never being INVOKED: the schedule disabled
by hand, deleted, or silently not firing, its invoke role broken, or the
account unable to run it. In that state there is no error, no alert and
no metric anywhere, because nothing ever executed. This function is the
only thing in the account that would notice.

It does NOT catch an invocation that runs and then fails, and it is
worth being exact about that, because the 2026-W30 and 2026-W31 outages
are the obvious thing to reach for as justification and they are the
wrong example. Those weeks died on EC2 InsufficientInstanceCapacity
inside a handler that had been invoked normally. AWS/Lambda Invocations
counts an invocation whether or not it succeeds, and the metric confirms
it: 2026-07-27 and 2026-08-03 each recorded Sum = 1.0. A canary reading
that window sees a non-zero count and reports healthy. What covers those
weeks is the Errors alarm on this function's target, added at the same
time as this one and absent when they happened.

So the two mechanisms are complements, not substitutes: Errors catches a
run that broke, this catches a run that never was. The second has never
happened here, which is exactly why it is worth instrumenting, since
nothing else in the account is watching for it at all.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

import boto3

_log = logging.getLogger()
_log.setLevel(logging.INFO)

cloudwatch = boto3.client("cloudwatch")
sns = boto3.client("sns")

FUNCTION_NAME = os.environ["WATCHED_FUNCTION_NAME"]
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]

#: How far back to count invocations.
#:
#: The schedule fires Tuesday 12:00 UTC; the run it checks for should
#: have started Monday 09:00 UTC (10:00 in CST), 26 to 27 hours earlier.
#: 36 hours reaches back to Monday 00:00 UTC, so the window contains the
#: whole of Monday and nothing else. Any Monday run is inside it however
#: late it started, and no other week's run can be.
#:
#: This is deliberately NOT the trailing 8 days the design note in
#: sns.tf proposed. Eight days reaches back to the *previous* Monday at
#: 12:00 UTC, which is after that week's scheduled start, so a prior
#: week that ran more than three hours late still counts as a datapoint
#: a week later and reports this week healthy when nothing ran at all.
#: For a dead man's switch a false negative is the only unacceptable
#: failure, so the window is sized to make one impossible rather than
#: to be generous.
#:
#: The cost of the shorter window is real and is accepted rather than
#: covered: a firing this function misses is not made up for by the next
#: one, and that week goes unchecked. Nothing detects it. The canary's
#: own Errors alarm cannot, because a firing that never happens emits no
#: Errors datapoint, and AWS/Scheduler TargetErrorCount cannot either,
#: because it is only emitted when a delivery is attempted and the
#: target errors, so a disabled or deleted schedule is silent to it.
#:
#: It is still the correct trade. A missed firing loses one week of
#: coverage; a masked miss reports healthy while a week is lost, which
#: is the failure the whole function exists to prevent.
LOOKBACK = dt.timedelta(hours=36)

#: GetMetricStatistics period. One day is the maximum the API accepts
#: and the coarsest useful resolution here: this function only needs the
#: sum over the window, not the shape of it. CloudWatch retains
#: 86400-second datapoints for 455 days, far beyond the lookback.
PERIOD_SECONDS = 86400


def _invocations_since(start: dt.datetime, end: dt.datetime) -> int:
    """Total Invocations for the watched function over the window.

    GetMetricStatistics omits periods that have no data rather than
    returning them as zero, so an empty ``Datapoints`` list is the
    representation of "it never ran". That is the case this function
    exists to detect, which makes the empty list the signal rather than
    an error to guard against.
    """
    resp = cloudwatch.get_metric_statistics(
        Namespace="AWS/Lambda",
        MetricName="Invocations",
        Dimensions=[{"Name": "FunctionName", "Value": FUNCTION_NAME}],
        StartTime=start,
        EndTime=end,
        Period=PERIOD_SECONDS,
        Statistics=["Sum"],
    )
    points = resp.get("Datapoints") or []
    return int(sum(p.get("Sum", 0.0) for p in points))


def _alert(subject: str, message: str) -> None:
    """Publish to the operator topic. Raises if it could not.

    Failing loudly here is the whole design. This function is only ever
    called on the path where a run was missed, so a publish that does
    not land means the one thing worth saying is unsaid. Logging it is
    not enough: a log line emits no AWS/Lambda Errors datapoint, there is
    no metric filter bridging this log group to a metric
    (``aws logs describe-metric-filters`` returns an empty list), and so
    a swallowed failure leaves Errors at zero and
    meridian-canary-errors silent. The account would then hold a canary
    that had correctly detected a missed week and told nobody.

    Re-raising makes Errors 1, which fires that alarm, which publishes
    through CloudWatch's own service principal on the topic's default
    policy. That is a different code path to this one and does not
    depend on this function's sns:Publish grant, which matters because
    AccessDenied is the most likely reason to be here (see the
    topic-policy warning in sns.tf).

    Nothing is lost by raising: Scheduler invokes this asynchronously
    and discards the return value.
    """
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=message)
    except Exception:
        _log.exception("canary could not publish to SNS; escalating via Errors")
        raise


def lambda_handler(event, context):  # noqa: ANN001, ARG001 - AWS signature
    end = dt.datetime.now(dt.timezone.utc)
    start = end - LOOKBACK
    hours = int(LOOKBACK.total_seconds() // 3600)

    count = _invocations_since(start, end)
    window = f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC"
    _log.info(
        "canary: %s invocations of %s in the last %dh (%s)",
        count, FUNCTION_NAME, hours, window,
    )

    if count > 0:
        return {"ok": True, "invocations": count, "window_hours": hours}

    _alert(
        f"[meridian] NO WEEKLY RUN in the last {hours}h",
        f"{FUNCTION_NAME} recorded zero invocations between {window}.\n"
        f"\n"
        f"Monday's run was never invoked at all. Not invoked and failed:\n"
        f"failed would have shown up as an invocation here and paged from\n"
        f"the Errors alarm instead. Nothing executed, so nothing sampled,\n"
        f"and there will be no manifest for this week unless it is re-fired\n"
        f"by hand before the archive moves on.\n"
        f"\n"
        f"Check, in order:\n"
        f"  aws scheduler get-schedule --name meridian-weekly --region us-east-2\n"
        f"    (State must be ENABLED. A disabled schedule emits nothing at all,\n"
        f"     including no TargetErrorCount, so no other check would have seen it.)\n"
        f"  aws logs tail /aws/lambda/{FUNCTION_NAME} --since 48h --region us-east-2\n"
        f"    (Empty means the function was never entered: the schedule, its\n"
        f"     invoke role, or the account is the fault, not the handler.)\n"
        f"\n"
        f"Recovery procedure: scripts/ec2-runbook.md\n",
    )
    return {"ok": False, "invocations": 0, "window_hours": hours}
