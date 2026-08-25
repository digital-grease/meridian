# Package the Lambda source. Single file, no external pip deps —
# boto3 is in the Lambda runtime by default.
data "archive_file" "orchestrator" {
  type        = "zip"
  source_file = "${path.module}/lambda/orchestrator.py"
  output_path = "${path.module}/lambda/orchestrator.zip"
}

# ---------- IAM for the Lambda --------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "meridian-orchestrator"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# Basic CloudWatch Logs (the AWS-managed policy is the canonical lift here).
resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "lambda_inline" {
  statement {
    sid    = "InstanceLifecycle"
    effect = "Allow"
    actions = [
      "ec2:StartInstances",
      "ec2:StopInstances",
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
    ]
    resources = ["*"]
    # Tighten via condition: only the cohabited instance.
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Name"
      values   = [var.instance_name_tag]
    }
  }

  # DescribeInstance* don't take resource-level conditions reliably; allow
  # them broadly but read-only.
  statement {
    sid    = "InstanceDescribe"
    effect = "Allow"
    actions = [
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "SsmCommand"
    effect = "Allow"
    actions = [
      "ssm:SendCommand",
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "AlertPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }

  # The orchestrator files a run-log record when a Monday dies before the
  # pipeline ever starts (see _record_failed_run in lambda/orchestrator.py).
  # 2026-W30 and 2026-W31 left no line in the append-only run log at all,
  # so the outage is not reproducible from the data. Write-only, and only
  # under the run_log/failures/ prefix: this Lambda has no business
  # touching raw samples or manifests.
  statement {
    sid       = "FailureRunLogWrite"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${local.bucket_arn}/${var.archive_bucket_prefix}run_log/failures/*"]
  }
}

resource "aws_iam_role_policy" "lambda_inline" {
  name   = "meridian-orchestrator-inline"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_inline.json
}

# ---------- The Lambda function -------------------------------------

resource "aws_lambda_function" "orchestrator" {
  function_name = "meridian-orchestrator"
  role          = aws_iam_role.lambda.arn
  runtime       = "python3.12"
  handler       = "orchestrator.lambda_handler"

  filename         = data.archive_file.orchestrator.output_path
  source_code_hash = data.archive_file.orchestrator.output_base64sha256

  # Generous: SSM polling + InstanceStatusOk wait + buffer. Lambda's max
  # is 900s (15 min) — that's why this Lambda uses async polling rather
  # than blocking on the wrapper script for the whole run.
  timeout     = 900
  memory_size = 256

  environment {
    variables = {
      INSTANCE_ID         = local.instance_id
      WRAPPER_SCRIPT_PATH = var.wrapper_script_path
      SNS_TOPIC_ARN       = aws_sns_topic.alerts.arn
      # Read by _send_wrapper as the SSM DELIVERY deadline. Until 2026-08
      # the function never read this at all and hardcoded 600.
      SSM_COMMAND_TIMEOUT_SECONDS = tostring(var.ssm_command_timeout_seconds)
      # The actual run ceiling, passed as the AWS-RunShellScript
      # `executionTimeout` document parameter. Unset means 3600 s, not
      # unlimited, which is what killed 2026-W34 mid-run.
      SSM_EXECUTION_TIMEOUT_SECONDS = tostring(var.ssm_execution_timeout_seconds)
      # Destination for the failure run-log record written when a Monday
      # dies before the pipeline starts. Leave the bucket empty to turn
      # the S3 write off; the record is still logged to CloudWatch.
      ARCHIVE_BUCKET        = var.archive_bucket_name
      ARCHIVE_BUCKET_PREFIX = var.archive_bucket_prefix
    }
  }
}

# Retain Lambda execution logs for 90 days.
#
# This was 14 days, which was shorter than the time it took to notice the
# outage it needed to explain. The 2026-07-27 log stream still lists in
# the console but returns zero events: the first outage week's forensic
# record had already aged out 18 days after the event, before anyone went
# looking, and only the 2026-08-03 stream survived to be read. Detection
# latency for a weekly job is measured in weeks, so retention has to be
# too. The whole log group is roughly 6 KB, so the cost argument for 14
# days never applied here.
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${aws_lambda_function.orchestrator.function_name}"
  retention_in_days = 90
}

# Async-invoke behaviour. THIS, not the Scheduler's retry_policy, is the
# layer that decides what happens when the function ERRORS.
#
# EventBridge Scheduler invokes Lambda asynchronously: Lambda returns 202
# as soon as it accepts the event, so Scheduler records a successful
# delivery and its retry_policy never observes a function error. (That
# policy still covers real delivery failures — throttling, 5xx from the
# Invoke API — so it is not useless, just not what retries a crash.)
#
# Confirmed empirically: on 2026-07-27 and 2026-08-03 the handler raised
# InsufficientInstanceCapacity and CloudWatch recorded exactly ONE
# Invocation each week, while the Scheduler policy was set to 1 retry.
# The retry that "should" have happened never did.
#
# The old value here was 0, so a failed weekly run got one attempt, ever.
# Two retries buys a few minutes against a transient blip. It explicitly
# does NOT ride out a multi-hour capacity outage — nothing at this layer
# can, since Lambda's async backoff is roughly 1 min then 2 min. For that
# the SNS alert plus the manual re-fire in scripts/ec2-runbook.md is the
# recovery path, and the honest answer is that the week may be lost.
#
# The on_failure destination covers the one gap the handler's own
# alerting cannot: a failure BEFORE the handler body runs (import error,
# missing env var, init timeout), where none of our code executes to
# publish. It overlaps with the in-handler alerts by design — worst case
# a few extra emails on a genuinely lost Monday, which beats silence.
resource "aws_lambda_function_event_invoke_config" "orchestrator" {
  function_name                = aws_lambda_function.orchestrator.function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 10800

  destination_config {
    on_failure {
      destination = aws_sns_topic.alerts.arn
    }
  }
}
