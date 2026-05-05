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
      INSTANCE_ID                 = local.instance_id
      WRAPPER_SCRIPT_PATH         = var.wrapper_script_path
      SNS_TOPIC_ARN               = aws_sns_topic.alerts.arn
      SSM_COMMAND_TIMEOUT_SECONDS = tostring(var.ssm_command_timeout_seconds)
    }
  }
}

# Retain Lambda execution logs for two weeks. CloudWatch defaults to
# never-expire which compounds cost over time.
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${aws_lambda_function.orchestrator.function_name}"
  retention_in_days = 14
}
