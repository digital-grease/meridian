# EventBridge Scheduler — newer than EventBridge Rules, supports
# IANA timezones natively (no DST math), and is the AWS-recommended
# replacement for cron-style scheduled rules.

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    # Tighten via SourceAccount to prevent the deputy-confused problem.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "meridian-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler_invoke" {
  statement {
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.orchestrator.arn]
  }
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name   = "invoke-orchestrator"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_invoke.json
}

resource "aws_scheduler_schedule" "weekly" {
  name = "meridian-weekly"
  flexible_time_window {
    mode = "OFF"
  }
  schedule_expression          = var.schedule_cron
  schedule_expression_timezone = var.schedule_timezone

  target {
    arn      = aws_lambda_function.orchestrator.arn
    role_arn = aws_iam_role.scheduler.arn
    # Empty payload — the Lambda doesn't read it. Keeps logs uncluttered.
    input = jsonencode({ source = "scheduler" })

    retry_policy {
      # Cron-trigger retries should be modest. If a fire fails entirely,
      # the SNS alert (or absence of expected weekly run-log entry) is
      # the recovery signal — re-firing the Scheduler hours later might
      # collide with specter's idle window.
      maximum_retry_attempts       = 1
      maximum_event_age_in_seconds = 3600
    }
  }
}
