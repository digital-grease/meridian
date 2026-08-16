# Durable raw-sample + manifest archive for the Meridian pipeline.
#
# Design goals (derived from CLAUDE.md hard rules):
#   * Raw samples are never overwritten or destroyed.
#   * No public access. Publication happens via GitHub Pages; this
#     bucket is the pipeline's durable backup, nothing more.
#   * Encryption at rest and HTTPS-only in transit, without effort from
#     the pipeline code.
#
# Object key layout (enforced by the pipeline, not this module). Every
# key is written under a common prefix, `meridian/`, which comes from
# `storage.s3.prefix` in meridian/config.yaml:
#   meridian/raw/{week_id}/{model_id}/{prompt_id}/samples.jsonl
#   meridian/manifests/{week_id}.json
#   meridian/manifests/latest.json

locals {
  # Mirrors `storage.s3.prefix` in meridian/config.yaml and
  # var.archive_bucket_prefix in ../ec2-cohabit. The lifecycle rules
  # below filter on it. This module carries no variable for it because
  # nothing else here is prefix-aware; if that changes, promote it.
  key_prefix = "meridian/"
}

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

  # Raw samples: storage-class transitions only. NOTHING under raw/ is
  # ever deleted by this configuration, neither current versions nor
  # non-current ones. See the DELETION note below before adding any
  # expiration block here.
  #
  # PREFIX: both rules filtered on "raw/" and "manifests/" until 2026-08.
  # No object has ever been keyed that way. Everything the pipeline
  # writes is under `meridian/`, so both rules matched nothing and this
  # whole configuration was inert.
  #
  # DELETION: this rule also carried a noncurrent_version_expiration of
  # var.expire_noncurrent_versions_days (90). While the prefix was wrong
  # that rule matched nothing, so it had never deleted anything in the
  # life of the bucket. Correcting the prefix would have armed a
  # first-ever permanent-delete rule against every raw key in the same
  # change that was described as a storage-class fix. A non-current
  # version of a raw samples.jsonl is what was there before a week was
  # re-run or re-uploaded over an existing key, which is to say it is
  # prior raw data, and "raw data is never destroyed, append-only,
  # retention forever" is a hard rule in CLAUDE.md with no cost
  # exception attached. It is dropped. Pruning non-current versions
  # stays on the manifests rule below, where the churn is a pointer file
  # and the intent was always to prune it.
  #
  # Before applying this prefix correction, look at what it will now
  # match:
  #   aws s3api list-object-versions --bucket meridian-archive-prod \
  #     --prefix meridian/raw/ --query 'Versions[?IsLatest==`false`]'
  # An empty result means no non-current raw versions exist today and
  # this was a near miss rather than a loss; a non-empty one is the list
  # of objects the old rule would have deleted 90 days after they were
  # superseded.
  #
  # STORAGE CLASS: the second transition read DEEP_ARCHIVE. Correcting
  # the prefix without also correcting that would have taken a dormant
  # misconfiguration and turned it into a live one, moving the oldest
  # raw samples behind 12-to-48-hour bulk restores. The project promises
  # that any published result is reproducible from the public raw data,
  # and an archive you have to file a restore request against and wait
  # two days for does not honour that promise. GLACIER_IR keeps
  # millisecond retrieval with no restore step. Do not put raw/ into
  # DEEP_ARCHIVE or GLACIER Flexible Retrieval without changing that
  # promise first.
  #
  # EXPECT NO EFFECT FROM THE TRANSITIONS TODAY. S3 lifecycle does not
  # transition objects smaller than 128 KB into STANDARD_IA,
  # ONEZONE_IA, INTELLIGENT_TIERING or GLACIER_IR. Raw objects here run
  # 9-93 KB (measured across the 90 samples.jsonl files in the local raw
  # tree on 2026-08-15; none is within 30 KB of the floor), so every one
  # of them is under it and both transitions below are a no-op for
  # essentially the whole prefix. That also means the earlier evidence
  # for the prefix bug, a 2026-W19 object over 100 days old still sitting
  # in STANDARD, is explained just as well by the size floor and is not
  # proof the prefix was the cause. The prefix fix is still correct, it
  # just does not buy the saving it was described as buying. Worse, an
  # object that does cross 128 KB is billed at the 128 KB minimum in
  # IA and GLACIER_IR plus a per-object transition charge, so at this
  # object-size profile the rule can cost more than it saves. The real
  # lever at this scale is fewer and larger objects, or
  # Intelligent-Tiering, not a transition rule that cannot fire. These
  # transitions stay for the day the object profile changes.
  #
  # var.transition_to_deep_archive_days keeps its name for now because
  # renaming a variable forces every operator's tfvars to change; it is
  # the second-transition day count, whatever the class.
  rule {
    id     = "raw-storage-class-transitions"
    status = "Enabled"
    filter {
      prefix = "${local.key_prefix}raw/"
    }
    transition {
      days          = var.transition_to_ia_days
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = var.transition_to_deep_archive_days
      storage_class = "GLACIER_IR"
    }
  }

  # Manifests are small and frequently read for diffing; keep them
  # in STANDARD and only reap non-current versions after the same
  # retention window. The `manifests/latest.json` pointer generates
  # non-current versions on every run; those are the main target.
  #
  # This is the ONLY rule in the module that deletes anything, and it is
  # deliberately confined to manifests. A superseded manifest is
  # regenerable from the raw samples plus the pipeline; a superseded raw
  # object is not regenerable from anything. Keep the asymmetry.
  rule {
    id     = "manifests-prune-noncurrent"
    status = "Enabled"
    filter {
      prefix = "${local.key_prefix}manifests/"
    }
    noncurrent_version_expiration {
      noncurrent_days = var.expire_noncurrent_versions_days
    }
  }
}
