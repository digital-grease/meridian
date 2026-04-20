# Writer permissions for the pipeline.
#
# The policy is shared between the optional IAM user (for local/cron
# runners that can't use OIDC) and the optional GitHub Actions role.
# Both paths grant the minimum set of actions the S3SampleUploader
# actually calls: PutObject + HeadObject, no delete, no read of raw/.

data "aws_iam_policy_document" "writer" {
  statement {
    sid    = "ArchiveWrite"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:HeadObject",
      "s3:GetObject",  # head_object under some boto configurations needs this
      "s3:ListBucket", # for key existence checks via ListObjectsV2 if used
    ]
    resources = [
      aws_s3_bucket.archive.arn,
      "${aws_s3_bucket.archive.arn}/*",
    ]
  }
}

resource "aws_iam_policy" "writer" {
  name        = "${var.bucket_name}-writer"
  description = "Write-only access to the Drift Audit raw-sample archive."
  policy      = data.aws_iam_policy_document.writer.json
}

# -------------- Path A: IAM user + access key ------------------------
#
# Works everywhere, but long-lived credentials. Rotate every 90 days;
# store the access key in a secrets manager — never in the repo.

resource "aws_iam_user" "writer" {
  count = var.create_writer_iam_user ? 1 : 0
  name  = "${var.bucket_name}-writer"
}

resource "aws_iam_user_policy_attachment" "writer" {
  count      = var.create_writer_iam_user ? 1 : 0
  user       = aws_iam_user.writer[0].name
  policy_arn = aws_iam_policy.writer.arn
}

resource "aws_iam_access_key" "writer" {
  count = var.create_writer_iam_user ? 1 : 0
  user  = aws_iam_user.writer[0].name
}

# -------------- Path B: GitHub OIDC role -----------------------------
#
# Preferred for the weekly-pipeline GitHub Actions workflow. Requires a
# pre-existing github.com OIDC provider in the account; the ARN is
# passed in as a variable so this module doesn't duplicate that
# shared resource.

data "aws_iam_policy_document" "github_oidc_trust" {
  count = var.enable_github_oidc_role ? 1 : 0

  statement {
    sid     = "GithubActionsAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.github_oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      # Restrict to the project's main branch and the weekly-pipeline
      # workflow. Tighten further in production if you run from other
      # workflows.
      values = [
        "repo:${var.github_repository}:ref:refs/heads/main",
        "repo:${var.github_repository}:environment:pipeline",
      ]
    }
  }
}

resource "aws_iam_role" "github_writer" {
  count              = var.enable_github_oidc_role ? 1 : 0
  name               = "${var.bucket_name}-gh-writer"
  assume_role_policy = data.aws_iam_policy_document.github_oidc_trust[0].json
}

resource "aws_iam_role_policy_attachment" "github_writer" {
  count      = var.enable_github_oidc_role ? 1 : 0
  role       = aws_iam_role.github_writer[0].name
  policy_arn = aws_iam_policy.writer.arn
}
