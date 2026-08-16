# Dead man's switch for the weekly run.
#
# Every other check in this module watches for a signal the pipeline
# emits, which means none of them can see a Monday that never starts:
# no error, no alert, and no metric datapoint to alarm on.
#
# Be precise about which hole this fills, because the obvious
# justification is the wrong one. 2026-W30 and 2026-W31 are NOT this
# case: those weeks were invoked normally and then raised on EC2
# capacity, and AWS/Lambda Invocations counts an invocation whether or
# not it succeeds. The metric still shows Sum = 1.0 on 2026-07-27 and
# 2026-08-03, so a canary would have read them as healthy. The Errors
# alarm in sns.tf is what covers that shape.
#
# What this covers is the schedule never firing at all: disabled by
# hand, deleted, its invoke role broken, or Scheduler unable to reach
# the function. Then there is no invocation, no error and no
# TargetErrorCount, and today literally nothing in the account would
# notice until the publish job 404s hours later. That has not happened
# yet, which is the point of instrumenting it before it does.
#
# Why a scheduled Lambda instead of a CloudWatch alarm: see the note in
# sns.tf section 2. The short version is that PutMetricAlarm caps an
# alarm's total evaluation period at one day, and one day of silence is
# the normal state for a weekly job six days out of seven.
#
# The handler's own rationale, including the lookback arithmetic, is in
# lambda/canary.py.

data "archive_file" "canary" {
  type        = "zip"
  source_file = "${path.module}/lambda/canary.py"
  output_path = "${path.module}/lambda/canary.zip"
}

# ---------- IAM -------------------------------------------------------

resource "aws_iam_role" "canary" {
  name = "meridian-canary"
  # Same Lambda service principal as the orchestrator; reuse its
  # assume-role document rather than restating it.
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "canary_basic" {
  role       = aws_iam_role.canary.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "canary_inline" {
  statement {
    sid    = "ReadInvocationMetric"
    effect = "Allow"
    # GetMetricStatistics is not resource-scoped: CloudWatch metric
    # reads take no resource ARN and no useful condition key, so "*" is
    # the only expressible form. It is read-only over metric data the
    # account already emits.
    actions   = ["cloudwatch:GetMetricStatistics"]
    resources = ["*"]
  }

  statement {
    sid       = "AlertPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_role_policy" "canary_inline" {
  name   = "meridian-canary-inline"
  role   = aws_iam_role.canary.id
  policy = data.aws_iam_policy_document.canary_inline.json
}

# ---------- The function ---------------------------------------------

resource "aws_lambda_function" "canary" {
  function_name = "meridian-canary"
  role          = aws_iam_role.canary.arn
  runtime       = "python3.12"
  handler       = "canary.lambda_handler"

  filename         = data.archive_file.canary.output_path
  source_code_hash = data.archive_file.canary.output_base64sha256

  # Two API calls and no waiting. The orchestrator's 900s is for SSM
  # polling; nothing here blocks.
  timeout     = 30
  memory_size = 128

  environment {
    variables = {
      WATCHED_FUNCTION_NAME = aws_lambda_function.orchestrator.function_name
      SNS_TOPIC_ARN         = aws_sns_topic.alerts.arn
    }
  }
}

# Same 90-day retention as the orchestrator, for the same reason: the
# detection latency on a weekly job is measured in weeks, so a log that
# ages out in 14 days cannot explain the incident it recorded.
resource "aws_cloudwatch_log_group" "canary" {
  name              = "/aws/lambda/${aws_lambda_function.canary.function_name}"
  retention_in_days = 90
}

# ---------- Trigger ---------------------------------------------------

# Tuesday 12:00 UTC, 27 hours after the Monday 09:00 UTC run it checks
# for. Deliberately not Monday evening: a run delayed by EC2 capacity
# retries can still be starting hours late (2026-W32 took seven
# StartInstances attempts across two invocations), and a canary that
# pages for a slow run instead of an absent one trains the operator to
# ignore it. A day of latency against the fortnight it actually took is
# the trade being made.
#
# UTC rather than America/Chicago on purpose. The orchestrator's own
# schedule is local time because its constraint is "before the working
# day"; this one's constraint is "a fixed interval after that run", and
# a DST shift would silently move the gap by an hour.
resource "aws_scheduler_schedule" "canary" {
  name = "meridian-canary"
  flexible_time_window {
    mode = "OFF"
  }
  schedule_expression          = "cron(0 12 ? * TUE *)"
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_lambda_function.canary.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "canary-schedule" })

    retry_policy {
      # Delivery retries only, same scope as the weekly schedule's.
      #
      # These matter more here than they look. The lookback is 36 hours
      # and the interval is a week, so a firing that never lands is NOT
      # made up for by the next one: that week simply goes unchecked and
      # nothing says so. Retry hard and allow a late delivery, since a
      # canary that runs some hours behind still answers the question
      # correctly, while one that never runs answers nothing.
      maximum_retry_attempts = 8
      # 10 hours. Anywhere inside Tuesday still leaves the window
      # covering the whole of Monday.
      maximum_event_age_in_seconds = 36000
    }
  }
}

# ---------- Watching the watcher --------------------------------------

# A canary that dies silently is worse than no canary, because the
# account then holds a resource that looks like coverage and is not.
# This is the same shape as the orchestrator's Errors alarm and is
# subject to no evaluation-period problem, because a function error is a
# datapoint rather than an absence.
#
# Note what this does NOT cover: the canary never being invoked at all.
# Turtles stop here by choice, and it is an ACCEPTED GAP rather than a
# covered one. Do not talk yourself out of that distinction later:
# TargetErrorCount is emitted only when a delivery is attempted and the
# target errors, so a schedule that is disabled or deleted is silent to
# it, and a firing that never happens emits no Errors datapoint either.
# If this canary's own schedule is turned off, nothing in the account
# notices.
#
# A second canary would have the identical gap one level up, so the
# recursion has to stop somewhere. It stops here because the failure it
# would cover (somebody disabling the watchdog) is deliberate action
# rather than drift, and the residual is one unchecked week rather than
# a silently wrong report.
resource "aws_cloudwatch_metric_alarm" "canary_errors" {
  alarm_name          = "meridian-canary-errors"
  alarm_description   = "meridian-canary raised. The dead man's switch is not working, so a missed weekly run would now go unreported. Check /aws/lambda/meridian-canary."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.canary.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
