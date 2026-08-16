# Alerting topic. Lambda publishes here on:
#   - Deferral (instance was already running when we tried to start it)
#   - Pre-flight contention (in-instance check tripped — GPU busy or
#     specter process detected)
#   - Wrapper script non-zero exit
#   - SSM command timeout
#
# Subscription confirmation: AWS sends an "Confirm subscription" email
# to var.alert_email after first apply. The user has to click the link;
# Terraform can't auto-confirm.

resource "aws_sns_topic" "alerts" {
  name = "meridian-pipeline-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------------------------------------------------------------------
# Absence-of-signal monitoring
#
# Everything above this line is an alert the pipeline emits about
# itself, which means it only works when the pipeline is alive enough
# to speak. Before 2026-08 `aws cloudwatch describe-alarms` on this
# account returned an empty list: there was no independent observer at
# all. On the two total-outage Mondays (2026-07-27 and 2026-08-03) the
# handler raised before it could publish anything, ZERO SNS messages
# went out, and the outage was found only because a downstream GitHub
# Actions job went red six hours later. Twice. The alarms below watch
# the pipeline from outside it, so a failure it cannot describe itself
# still reaches someone. Note the gap they do NOT close: an invocation
# that never happens at all emits no metric to alarm on, which is what
# the absent dead man's switch in section 2 was for.
#
# Same-account CloudWatch to SNS needs no topic policy; the default SNS
# policy already permits it. Do not add an aws_sns_topic_policy here
# without also re-granting the Lambda and the instance role, since a
# resource policy replaces the default rather than adding to it.
# ---------------------------------------------------------------------

# 1. The function errored. Catches everything the handler re-raises,
#    including the InsufficientInstanceCapacity path, and does not
#    depend on the handler's own sns:Publish succeeding.
resource "aws_cloudwatch_metric_alarm" "orchestrator_errors" {
  alarm_name          = "meridian-orchestrator-errors"
  alarm_description   = "meridian-orchestrator raised. Check /aws/lambda/meridian-orchestrator for the reason, then scripts/ec2-runbook.md for the manual re-fire."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.orchestrator.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  # No datapoint means no invocation, which is a different failure with
  # a different remedy (section 2 below). Do not double-report it here:
  # this alarm would then fire every day of a healthy week.
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_sns_topic.alerts.arn]
  ok_actions         = [aws_sns_topic.alerts.arn]
}

# 2. NOT PRESENT: the dead man's switch on "nothing invoked us".
#
#    This is the one check that would have caught 2026-W30 and
#    2026-W31 directly, and it is deliberately absent rather than
#    forgotten. Read this before adding it back.
#
#    It was written here as a metric alarm on AWS/Lambda Invocations
#    with period = 86400, evaluation_periods = 7, datapoints_to_alarm =
#    7 and treat_missing_data = "breaching", i.e. "the trailing seven
#    1-day windows all contained zero invocations". The logic is right;
#    the resource is not applyable. PutMetricAlarm enforces an API-side
#    rule that terraform validate cannot see: "An alarm's total current
#    evaluation period can be no longer than one day, so this number
#    multiplied by Period cannot be more than 86,400 seconds." Seven
#    days of one-day periods is 604,800 seconds, so the API rejects it
#    with a ValidationError and the whole apply goes red. There is no
#    variation that gets around it either: a weekly job cannot be
#    watched by an alarm whose entire memory is 24 hours, because 24
#    hours of silence is the normal state six days out of seven.
#
#    The replacement is a SCHEDULED CANARY, which is not subject to the
#    one-day rule because it is not an alarm at all: an EventBridge
#    schedule fires a small Lambda every Tuesday around 12:00 UTC, a few
#    hours after Monday's run should have started. The canary calls
#    cloudwatch:GetMetricStatistics for AWS/Lambda Invocations on
#    meridian-orchestrator over the trailing 8 days, sums the
#    datapoints, and publishes to this topic if the sum is zero.
#    Detection latency is the same 15-24 hours the alarm would have
#    given, against the 14 days it actually took in July. What it needs:
#    an execution role carrying AWSLambdaBasicExecutionRole,
#    cloudwatch:GetMetricStatistics on "*" since that call takes no
#    resource-level permissions, and sns:Publish on
#    aws_sns_topic.alerts.arn; a log group with the same 90-day
#    retention as the orchestrator's; and a trigger, either an
#    aws_scheduler_schedule with an invoke role or an
#    aws_cloudwatch_event_rule with an aws_lambda_permission.
#
#    It is not landed here because it cannot be verified before the
#    2026-08-17 run: a new function, a new role and a new schedule that
#    have never been planned or applied are a poor trade against an
#    apply that must succeed this weekend. Removing the broken alarm
#    regresses nothing that existed before 2026-08, since the account
#    had no alarms at all until this file; alarms 1 and 3 and the
#    on_failure destination in lambda.tf all still land.
#
#    Until the canary exists, absence of a run is caught only by the
#    downstream publish workflow going red, which is what happened on
#    2026-07-27 and 2026-08-03 and took two weeks.

# 3. EventBridge Scheduler could not deliver to its target. Distinct
#    from alarm 1: the function never ran, so it emitted neither an
#    Error nor an alert of its own.
#
#    AWS/Scheduler exposes ScheduleGroup as its only dimension, with no
#    per-schedule breakdown, so this alarm covers every schedule in the
#    group. meridian-weekly is the sole occupant of the default group
#    today; if another schedule is ever added there, move meridian's to
#    its own group and re-dimension this alarm, or it will page for
#    somebody else's failure.
resource "aws_cloudwatch_metric_alarm" "scheduler_target_errors" {
  alarm_name          = "meridian-scheduler-target-errors"
  alarm_description   = "EventBridge Scheduler failed to invoke the meridian orchestrator (schedule ${aws_scheduler_schedule.weekly.name}, group ${coalesce(aws_scheduler_schedule.weekly.group_name, "default")}). The Monday run did not start."
  namespace           = "AWS/Scheduler"
  metric_name         = "TargetErrorCount"
  dimensions          = { ScheduleGroup = coalesce(aws_scheduler_schedule.weekly.group_name, "default") }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
