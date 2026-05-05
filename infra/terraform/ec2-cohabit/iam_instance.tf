# Extra policies attached to specter's existing IAM role. We never own
# the role itself — that's specter's. Removing this Terraform module
# leaves the role intact (only the inline policies disappear).

data "aws_iam_policy_document" "instance_meridian_s3_write" {
  statement {
    sid    = "MeridianArchiveWrite"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject", # download for snapshot rebuild + idempotency checks
      "s3:HeadObject",
      "s3:ListBucket",
      "s3:AbortMultipartUpload",
    ]
    resources = [
      local.bucket_arn,
      "${local.bucket_arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "instance_meridian_s3" {
  name   = "meridian-instance-archive-write"
  role   = var.instance_role_name
  policy = data.aws_iam_policy_document.instance_meridian_s3_write.json
}

# SSM Parameter Store read access for the API keys. Decryption permission
# is implicit when WithDecryption=True (uses the AWS-managed KMS key
# `aws/ssm`). If the parameters are encrypted with a customer-managed
# CMK in the future, add a kms:Decrypt allow on the CMK arn here.
data "aws_iam_policy_document" "instance_meridian_ssm_read" {
  statement {
    sid       = "MeridianSecretsRead"
    effect    = "Allow"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = local.ssm_param_arns
  }
}

resource "aws_iam_role_policy" "instance_meridian_ssm" {
  name   = "meridian-instance-secrets-read"
  role   = var.instance_role_name
  policy = data.aws_iam_policy_document.instance_meridian_ssm_read.json
}

# The wrapper script publishes its own start / success / failure / defer
# notifications to the alerts topic and stops the instance after it
# finishes (when WE_OWN_LIFECYCLE=1). Both capabilities live with the
# wrapper rather than the orchestrator Lambda because Lambda's 15-min
# timeout can't sit through a 30-90 min pipeline run.
data "aws_iam_policy_document" "instance_meridian_ops" {
  statement {
    sid       = "MeridianAlertPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }

  statement {
    sid     = "MeridianStopSelf"
    effect  = "Allow"
    actions = ["ec2:StopInstances"]
    # Restrict to instances tagged Name=specter so an exfiltrated key
    # can't stop unrelated infra.
    resources = ["arn:${data.aws_partition.current.partition}:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Name"
      values   = [var.instance_name_tag]
    }
  }
}

resource "aws_iam_role_policy" "instance_meridian_ops" {
  name   = "meridian-instance-ops"
  role   = var.instance_role_name
  policy = data.aws_iam_policy_document.instance_meridian_ops.json
}
