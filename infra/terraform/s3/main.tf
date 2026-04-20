# Durable raw-sample + manifest archive for the Drift Audit pipeline.
#
# Design goals (derived from CLAUDE.md hard rules):
#   * Raw samples are never overwritten or destroyed.
#   * No public access. Publication happens via GitHub Pages; this
#     bucket is the pipeline's durable backup, nothing more.
#   * Encryption at rest and HTTPS-only in transit, without effort from
#     the pipeline code.
#
# Object key layout (enforced by the pipeline, not this module):
#   raw/{week_id}/{model_id}/{prompt_id}/samples.jsonl
#   manifests/{week_id}.json
#   manifests/latest.json

resource "aws_s3_bucket" "archive" {
  bucket = var.bucket_name

  # The hard rule says raw data is never destroyed; Terraform must never
  # accidentally remove the bucket on state drift. Re-enable temporarily
  # only under human supervision.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_ownership_controls" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "archive" {
  bucket                  = aws_s3_bucket.archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "archive" {
  bucket = aws_s3_bucket.archive.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# HTTPS-only: deny any request that isn't TLS. Belt-and-suspenders with
# the public-access-block above, since bucket policies are the only
# protection if someone accidentally opens the block.
data "aws_iam_policy_document" "bucket_policy" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.archive.arn,
      "${aws_s3_bucket.archive.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "archive" {
  bucket = aws_s3_bucket.archive.id
  policy = data.aws_iam_policy_document.bucket_policy.json
}

resource "aws_s3_bucket_lifecycle_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id

  # Raw samples: transition storage classes over time to keep cost flat
  # as the archive grows. Current versions are never expired — that
  # would violate the append-only durability guarantee.
  rule {
    id     = "raw-storage-class-transitions"
    status = "Enabled"
    filter {
      prefix = "raw/"
    }
    transition {
      days          = var.transition_to_ia_days
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = var.transition_to_deep_archive_days
      storage_class = "DEEP_ARCHIVE"
    }
    noncurrent_version_expiration {
      noncurrent_days = var.expire_noncurrent_versions_days
    }
  }

  # Manifests are small and frequently read for diffing; keep them
  # in STANDARD and only reap non-current versions after the same
  # retention window. The `manifests/latest.json` pointer generates
  # non-current versions on every run; those are the main target.
  rule {
    id     = "manifests-prune-noncurrent"
    status = "Enabled"
    filter {
      prefix = "manifests/"
    }
    noncurrent_version_expiration {
      noncurrent_days = var.expire_noncurrent_versions_days
    }
  }
}
