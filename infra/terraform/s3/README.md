# `infra/terraform/s3` — durable raw-sample archive

Provisions the S3 bucket the Meridian pipeline mirrors raw samples and
published manifests into, plus the IAM permissions needed to write to it.

This is the *durability* backup. Public distribution happens via GitHub Pages
under `/data/{week}/`; the bucket itself is private and blocked from public
access. The hard rule from `CLAUDE.md` — "raw data is never destroyed" —
maps here to versioning + a lifecycle that only moves objects between storage
classes, never expires current versions.

## What you get

- Private, versioned S3 bucket with SSE-S3 encryption and an HTTPS-only
  bucket policy.
- Lifecycle rule on `raw/`: `STANDARD` → `STANDARD_IA` (30 d) → `DEEP_ARCHIVE`
  (365 d). Current versions are never expired.
- Lifecycle rule on `manifests/`: non-current versions expire after 90 d to
  keep the pointer-churn on `manifests/latest.json` from accumulating cost.
- IAM policy granting `PutObject` + `HeadObject` + `ListBucket` against the
  bucket — no deletes, no reads of other buckets.
- Your choice of two writer-principal patterns:
  - **IAM user + access key** (default). Works anywhere; requires key rotation.
  - **GitHub Actions OIDC role** (opt-in via `enable_github_oidc_role`).
    Preferred when the weekly pipeline runs in GitHub Actions.

## Apply

```bash
cd infra/terraform/s3
terraform init
terraform plan -var bucket_name=meridian-archive-prod
terraform apply -var bucket_name=meridian-archive-prod
```

The bucket has `prevent_destroy = true`. Taking the bucket down intentionally
requires removing that line and a second targeted apply.

## Wire the pipeline

Add the bucket to `meridian/config.yaml`:

```yaml
storage:
  raw_dir: "data/raw"
  s3:
    bucket: "meridian-archive-prod"
    region: "us-east-1"
    prefix: ""                      # or "meridian/" to namespace within a shared bucket
    publish_latest_pointer: true    # write manifests/latest.json each run
```

Then provide AWS credentials to the process that runs `uv run python -m
meridian.pipeline.cli run` via the standard boto3 chain (env vars, instance
profile, OIDC). The repo never stores credentials.

### Path A: local / cron host

Create the IAM user (default in this module), grab the access key:

```bash
terraform output -raw writer_access_key_id
terraform output -raw writer_secret_access_key  # mark as sensitive; store elsewhere
```

Export them where the pipeline runs:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-east-1
```

Rotate every 90 days. An access-key-age check in the runbook belongs on your
own calendar, not in this module.

### Path B: GitHub Actions (preferred)

```bash
terraform apply \
  -var bucket_name=meridian-archive-prod \
  -var enable_github_oidc_role=true \
  -var create_writer_iam_user=false \
  -var github_oidc_provider_arn=arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com \
  -var github_repository=digital-grease/meridian
```

Take the output role ARN and add it to `.github/workflows/weekly-pipeline.yml`:

```yaml
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_TO_ASSUME }}
          aws-region: us-east-1
```

No long-lived keys anywhere.

## Threat model notes

- **Bucket takeover via typosquatted name.** Bucket names are globally unique
  and, once deleted, reusable by anyone. The `prevent_destroy` lifecycle rule
  blocks accidental deletion via Terraform; belt-and-suspenders, apply an
  account-level deny on `s3:DeleteBucket` for the archive bucket ARN through
  your existing guardrail SCP.
- **Key leak.** The IAM policy is write-only for the archive prefix. A leaked
  key can write garbage but cannot delete or read prior samples. Still: rotate.
- **Public exposure.** `BucketOwnerEnforced` + a public-access-block + the
  HTTPS-only bucket policy together make accidental public exposure difficult.
  If you ever need to publish objects (v2 of the project), create a second
  bucket for that — don't relax this one.
- **Integrity.** S3 versioning catches the rare "same key overwritten with
  different content" failure mode. The pipeline's idempotency check uses the
  object ETag (MD5 of the body for single-part uploads) so it short-circuits
  uploads whose content is unchanged.

## What this module does *not* do

- Cross-region replication. Out of scope for v1; revisit when the archive
  grows past a few GB or when regulatory requirements demand it.
- Public read access for `/data/` distribution. GitHub Pages handles that
  today; S3 is archive-only here.
- CloudTrail data events. A separate account-level concern; enable via your
  audit-account Terraform, not this module.
- Budget alerts. Use AWS Budgets in the same account; the lifecycle rules
  here mean cost should be dominated by DEEP_ARCHIVE after a year.
