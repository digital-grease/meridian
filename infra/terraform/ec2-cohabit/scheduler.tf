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
      # Widened after 2026-W30/W31 were both lost to a single
      # InsufficientInstanceCapacity on StartInstances: one retry four
      # minutes later is no use against a capacity crunch, which lasts
      # hours.
      #
      # The event-age cap is set so the last retry still leaves room for
      # a ~30-90 min run before the publish workflow reads S3 at 13:00
      # UTC. Schedule fires 09:00 UTC (04:00 America/Chicago), so three
      # hours is the usable window.
      #
      # Colliding with specter is not the risk the old comment feared:
      # the wrapper's pre-flight (GPU-busy and specter-process checks in
      # run-weekly.sh) already defers cleanly if specter has the box, and
      # the Lambda defers if the instance is already running.
      #
      # Honest limit: Scheduler backs off exponentially and may well
      # exhaust these attempts inside the first hour, so this does NOT
      # reliably cover a multi-hour outage. It buys short-crunch
      # resilience. The actual recovery path for a long outage is an
      # operator acting on the SNS alert (see scripts/ec2-runbook.md);
      # that alert is the fix here, this is the cheap complement.
      maximum_retry_attempts       = 4
      maximum_event_age_in_seconds = 10800
    }
  }
}
