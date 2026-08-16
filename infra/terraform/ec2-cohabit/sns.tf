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

# 2. The dead man's switch on "nothing invoked us", added 2026-08-16.
#    Lives in canary.tf.
#
#    It is a scheduled Lambda rather than an alarm for a reason worth
#    keeping written down, because the alarm form is the obvious thing
#    to reach for and it does not work.
#
#    Note it does NOT cover 2026-W30 and 2026-W31, despite being written
#    in response to them. Those weeks were invoked and then raised, and
#    Invocations counts a failed invocation the same as a successful one
#    (the metric still reads Sum = 1.0 for both Mondays). Alarm 1 above
#    is what catches that. This covers the disjoint case where nothing
#    is invoked at all, which alarm 1 cannot see because a function that
#    never runs emits no Errors datapoint either.
#
#    It was written here first as a metric alarm on AWS/Lambda
#    Invocations with period = 86400, evaluation_periods = 7,
#    datapoints_to_alarm = 7 and treat_missing_data = "breaching", i.e.
#    "the trailing seven 1-day windows all contained zero invocations".
#    The logic is right; the resource is not applyable. PutMetricAlarm
#    enforces an API-side rule that terraform validate cannot see: "An
#    alarm's total current evaluation period can be no longer than one
#    day, so this number multiplied by Period cannot be more than 86,400
#    seconds." Seven days of one-day periods is 604,800 seconds, so the
#    API rejects it with a ValidationError and the whole apply goes red.
#    No variation escapes the rule: a weekly job cannot be watched by an
#    alarm whose entire memory is 24 hours, because 24 hours of silence
#    is the normal state six days out of seven.
#
#    A scheduled function has no such constraint, because it is not an
#    alarm and can look back as far as it likes. See canary.tf.

# 3. EventBridge Scheduler could not deliver to its target. Distinct
#    from alarm 1: the function never ran, so it emitted neither an
#    Error nor an alert of its own.
#
#    AWS/Scheduler exposes ScheduleGroup as its only dimension, with no
#    per-schedule breakdown, so this alarm covers every schedule in the
#    group and cannot say which one failed.
#
#    As of 2026-08-16 the default group holds two meridian schedules,
#    meridian-weekly and meridian-canary, and this alarm deliberately
#    covers both: the earlier note here said to split them into separate
#    groups if a second was ever added, and that advice is withdrawn.
#    Both are meridian's, so a page for either is a page for us, and one
#    alarm over the pair is cheaper than two alarms plus two groups. The
#    original concern stands only for a schedule belonging to something
#    else, which would page us for a stranger's failure; keep those out
#    of the default group.
#
#    The description therefore names the group and both candidates
#    rather than asserting which run was lost.
resource "aws_cloudwatch_metric_alarm" "scheduler_target_errors" {
  alarm_name          = "meridian-scheduler-target-errors"
  alarm_description   = "EventBridge Scheduler failed to deliver to a target in group ${coalesce(aws_scheduler_schedule.weekly.group_name, "default")}. Either the Monday orchestrator run (${aws_scheduler_schedule.weekly.name}) or the Tuesday dead man's switch (${aws_scheduler_schedule.canary.name}) did not start; the metric has no per-schedule dimension, so check both."
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
