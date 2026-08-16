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
    effect  = "Allow"
    actions = ["lambda:InvokeFunction"]
    # Both scheduled functions run under this one role: the Monday
    # orchestrator and the Tuesday dead man's switch (canary.tf). The
    # policy resource keeps its original "invoke-orchestrator" name so
    # adding the canary does not churn an inline policy that predates
    # it; the name is now narrower than the grant.
    resources = [
      aws_lambda_function.orchestrator.arn,
      aws_lambda_function.canary.arn,
    ]
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
      # SCOPE: this covers Scheduler's own DELIVERY failures only —
      # throttling, or a 5xx from the Lambda Invoke API. It does NOT
      # retry a function error. Scheduler invokes Lambda asynchronously
      # and gets a 202 the instant Lambda accepts the event, so a
      # handler that raises still looks like a successful delivery here.
      #
      # Verified the hard way: during the 2026-W30/W31 capacity outage
      # this policy was set to 1 retry, the handler raised both weeks,
      # and CloudWatch recorded exactly one Invocation each Monday. The
      # retry never fired.
      #
      # Function-error retries live on the async-invoke config in
      # lambda.tf. Change them there, not here.
      #
      # These values are generous because delivery retries are cheap and
      # idempotent at this point in the flow (nothing has started yet).
      # The event-age cap keeps a late delivery from starting a run so
      # close to the 13:00 UTC publish read that it cannot finish.
      maximum_retry_attempts       = 4
      maximum_event_age_in_seconds = 10800
    }
  }
}
