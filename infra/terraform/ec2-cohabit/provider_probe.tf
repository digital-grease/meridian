# Sunday check that Monday's run can pay for itself.
#
# Anthropic credit is prepaid with auto-reload off on purpose, and an
# empty balance is silent until a request is refused. 2026-W36 and
# 2026-W38 were lost that way: the pipeline's own preflight noticed at
# 09:00 UTC on the Monday, which is exactly when it stops being
# fixable. This asks the same question about 21 hours earlier, with one
# tiny real request per model, and emails only when something is wrong.
#
# It never touches EC2. The instance stays stopped, so this neither
# depends on nor interferes with the reaper or any other stop
# mechanism.
#
# The handler's rationale and the status taxonomy are in
# lambda/provider_probe.py.

data "archive_file" "provider_probe" {
  type        = "zip"
  source_file = "${path.module}/lambda/provider_probe.py"
  output_path = "${path.module}/lambda/provider_probe.zip"
}

# ---------- IAM -------------------------------------------------------

resource "aws_iam_role" "provider_probe" {
  name               = "meridian-provider-probe"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "provider_probe_basic" {
  role       = aws_iam_role.provider_probe.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "provider_probe_inline" {
  statement {
    sid    = "ReadProviderKeys"
    effect = "Allow"
    # The same two parameters the instance role reads, and nothing else.
    # GetParameter only: the function reads one name at a time, so the
    # instance role's GetParameters is not needed here. No kms:Decrypt,
    # for the reason given in iam_instance.tf: the parameters use the
    # AWS-managed aws/ssm key, whose policy already allows decryption
    # through SSM. A move to a CMK has to add the grant here as well.
    actions   = ["ssm:GetParameter"]
    resources = local.ssm_param_arns
  }

  statement {
    sid       = "AlertPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_role_policy" "provider_probe_inline" {
  name   = "meridian-provider-probe-inline"
  role   = aws_iam_role.provider_probe.id
  policy = data.aws_iam_policy_document.provider_probe_inline.json
}

# ---------- The function ---------------------------------------------

resource "aws_lambda_function" "provider_probe" {
  function_name = "meridian-provider-probe"
  role          = aws_iam_role.provider_probe.arn
  runtime       = "python3.12"
  handler       = "provider_probe.lambda_handler"

  filename         = data.archive_file.provider_probe.output_path
  source_code_hash = data.archive_file.provider_probe.output_base64sha256

  # Targets are probed one after another. Worst case per target is two
  # 30 s requests plus a 10 s capped backoff, about 70 s, so the six
  # targets of the 2026-10 roster succession (both retiring and both
  # incoming models probed in the overlap weeks, plus the classifier)
  # can need about 420 s when a provider is hanging. 600 leaves room for
  # one more target and stays under Lambda's 900 s limit. A timeout here
  # raises Errors and pages through the alarm below, which is the right
  # outcome for a probe that could not finish.
  timeout     = 600
  memory_size = 128

  environment {
    variables = {
      SNS_TOPIC_ARN       = aws_sns_topic.alerts.arn
      ANTHROPIC_KEY_PARAM = aws_ssm_parameter.anthropic_api_key.name
      OPENAI_KEY_PARAM    = aws_ssm_parameter.openai_api_key.name
      PROBE_TARGETS       = jsonencode(var.provider_probe_targets)
    }
  }
}

resource "aws_cloudwatch_log_group" "provider_probe" {
  name              = "/aws/lambda/${aws_lambda_function.provider_probe.function_name}"
  retention_in_days = 90
}

# ---------- Trigger ---------------------------------------------------

# Sunday 12:00 UTC, 21 hours before the earliest Monday start (09:00 UTC
# in CDT, 10:00 UTC in CST). Early enough to top up during a normal
# Sunday; late enough that most of the week's spending elsewhere has
# already happened, so a pass means something about Monday.
#
# UTC for the same reason as the canary: the constraint is an interval
# before the run, not a local time of day.
resource "aws_scheduler_schedule" "provider_probe" {
  name = "meridian-provider-probe"
  flexible_time_window {
    mode = "OFF"
  }
  schedule_expression          = "cron(0 12 ? * SUN *)"
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_lambda_function.provider_probe.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "provider-probe-schedule" })

    retry_policy {
      # A firing that never lands is not made up for by the next one, a
      # week later, so retry delivery hard. Six hours still leaves at
      # least fifteen before Monday to act on what it finds.
      maximum_retry_attempts       = 8
      maximum_event_age_in_seconds = 21600
    }
  }
}

# ---------- Watching the watcher --------------------------------------

# Same shape as canary_errors. The handler reports every provider
# outcome as data and raises only when it cannot do its job: a broken
# PROBE_TARGETS at cold start, a timeout, or an SNS publish that failed
# after it found something. Each of those is otherwise silent.
resource "aws_cloudwatch_metric_alarm" "provider_probe_errors" {
  alarm_name          = "meridian-provider-probe-errors"
  alarm_description   = "meridian-provider-probe raised. The Sunday credit and credential check did not complete, so an empty balance or a dead key would go unnoticed until Monday's run. Check /aws/lambda/meridian-provider-probe."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.provider_probe.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
