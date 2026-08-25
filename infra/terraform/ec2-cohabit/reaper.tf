# Backstop for the instance stop that scripts/run-weekly.sh owns.
#
# The wrapper stops its own instance on every exit path it can reach,
# which is every path except the ones that kill it outright. 2026-W34
# found one: SSM's AWS-RunShellScript carries a 3600 s default
# executionTimeout that the orchestrator never overrode, so a run that
# needed about 2h10m was SIGKILLed at the hour mark. SIGKILL cannot be
# trapped, self_stop_if_needed never ran, and the g5.2xlarge billed
# roughly eighteen idle hours before anyone looked.
#
# orchestrator.py now sets executionTimeout explicitly, which closes
# that particular hole. This closes the shape of hole: an OOM kill, a
# panic, or the next unexamined AWS default all end the same way, with a
# wrapper that is not running and therefore cannot stop anything. The
# self-stop stays the primary mechanism; this only ever fires when the
# primary already failed, and every firing is worth reading as a bug
# report rather than as the system working.
#
# The safety argument for running this against an instance shared with
# specter is in lambda/reaper.py, and it is worth reading before
# changing anything here: the gate is OWNERSHIP (did meridian start this
# boot and is meridian's run over), not idleness. Idleness is only a
# second check against a race. A reaper gated on idleness alone would
# eventually stop a live specter session.

data "archive_file" "reaper" {
  type        = "zip"
  source_file = "${path.module}/lambda/reaper.py"
  output_path = "${path.module}/lambda/reaper.zip"
}

# ---------- IAM -------------------------------------------------------

resource "aws_iam_role" "reaper" {
  name               = "meridian-reaper"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "reaper_basic" {
  role       = aws_iam_role.reaper.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "reaper_inline" {
  statement {
    sid    = "DescribeInstances"
    effect = "Allow"
    # ec2:DescribeInstances takes no resource ARN; "*" is the only
    # expressible form. Read-only.
    actions   = ["ec2:Describe*"]
    resources = ["*"]
  }

  statement {
    sid    = "StopOnlyTheCohabitInstance"
    effect = "Allow"
    # Scoped to the single instance on purpose. This function's entire
    # job is stopping one specific box, and a wildcard here would let a
    # bug in the ownership logic reach every instance in the account.
    actions   = ["ec2:StopInstances"]
    resources = ["arn:${data.aws_partition.current.partition}:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/${local.instance_id}"]
  }

  statement {
    sid    = "ReadDispatchHistory"
    effect = "Allow"
    # How the function decides whose boot this is.
    actions = [
      "ssm:ListCommandInvocations",
      "ssm:GetCommandInvocation",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "RunIdlenessProbe"
    effect = "Allow"
    # SendCommand is the one genuinely powerful grant here, so it is
    # scoped to the one instance AND the one document. Without the
    # document constraint this role could run any SSM document,
    # including ones that install software.
    actions = ["ssm:SendCommand"]
    resources = [
      "arn:${data.aws_partition.current.partition}:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/${local.instance_id}",
      "arn:${data.aws_partition.current.partition}:ssm:${var.region}::document/AWS-RunShellScript",
    ]
  }

  statement {
    sid       = "AlertPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_role_policy" "reaper_inline" {
  name   = "meridian-reaper-inline"
  role   = aws_iam_role.reaper.id
  policy = data.aws_iam_policy_document.reaper_inline.json
}

# ---------- The function ---------------------------------------------

resource "aws_lambda_function" "reaper" {
  function_name = "meridian-reaper"
  role          = aws_iam_role.reaper.arn
  runtime       = "python3.12"
  handler       = "reaper.lambda_handler"

  filename         = data.archive_file.reaper.output_path
  source_code_hash = data.archive_file.reaper.output_base64sha256

  # Long enough to cover the SSM probe round-trip (PROBE_TIMEOUT_SECONDS
  # plus agent latency) with headroom, and nothing else here blocks.
  timeout     = 180
  memory_size = 128

  environment {
    variables = {
      INSTANCE_ID   = local.instance_id
      SNS_TOPIC_ARN = aws_sns_topic.alerts.arn
      # Must match the Comment in orchestrator.py's send_command. This
      # string IS the ownership signal: drift between the two turns
      # every reap into a permanent "not meridian's boot" no-op, which
      # fails silently in the safe direction and is therefore easy to
      # miss. The orchestrator tests assert the literal.
      DISPATCH_COMMENT_MATCH = "meridian weekly pipeline"
      MIN_UPTIME_SECONDS     = tostring(var.reaper_min_uptime_seconds)
      # Same threshold as the wrapper's pre-flight, so "busy" means one
      # thing across the system.
      GPU_MEMORY_THRESHOLD_MB = tostring(var.gpu_memory_threshold_mb)
    }
  }
}

resource "aws_cloudwatch_log_group" "reaper" {
  name              = "/aws/lambda/${aws_lambda_function.reaper.function_name}"
  retention_in_days = 90
}

# ---------- Trigger ---------------------------------------------------

# Hourly, every day, not just Mondays.
#
# The cost of a missed stop is linear in time, so detection latency is
# the entire value of this function: an hourly check turns an
# eighteen-hour overrun into a one-hour one. Running it every day rather
# than only around the weekly window matters because a manually
# dispatched run, a re-run after a capacity failure, or an operator
# invoking the orchestrator by hand can leave the box running on any day
# of the week.
#
# On the roughly 167 hourly firings a week where nothing is wrong, the
# function makes one DescribeInstances call, sees a stopped instance,
# and returns. That is free in every sense that matters.
resource "aws_scheduler_schedule" "reaper" {
  name = "meridian-reaper"
  flexible_time_window {
    mode = "OFF"
  }
  schedule_expression          = "rate(1 hour)"
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_lambda_function.reaper.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "reaper-schedule" })

    retry_policy {
      # Retries matter far less here than on the canary: the next
      # firing is an hour away rather than a week, and it asks the
      # identical question. Keep them low and short so a transient
      # failure does not queue up a pile of redundant invocations.
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 900
    }
  }
}

# ---------- Watching the watcher --------------------------------------

# Same shape and same reasoning as the canary's Errors alarm: a reaper
# that throws on every firing looks exactly like a reaper with nothing
# to do, since both are silent. The difference is a metric datapoint,
# so alarm on it.
#
# treat_missing_data is "notBreaching" because a healthy hour emits no
# Errors datapoint at all.
resource "aws_cloudwatch_metric_alarm" "reaper_errors" {
  alarm_name          = "meridian-reaper-errors"
  alarm_description   = "meridian-reaper raised. The instance stop backstop is not working, so a weekly run that dies uncleanly would leave the g5.2xlarge billing unnoticed. Check /aws/lambda/meridian-reaper."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.reaper.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
