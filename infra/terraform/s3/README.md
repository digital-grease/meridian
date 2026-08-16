# `infra/terraform/s3` — durable raw-sample archive

Provisions the S3 bucket the Meridian pipeline mirrors raw samples and
published manifests into, plus the IAM permissions needed to write to it.

This is the *durability* backup. Public distribution happens via GitHub Pages
under `/data/{week}/`; the bucket itself is private and blocked from public
access. The hard rule from `CLAUDE.md`, "raw data is never destroyed", maps
here to versioning plus a lifecycle that, under `raw/`, only moves objects
between storage classes and never expires anything, current or non-current.

## What you get

- Private, versioned S3 bucket with SSE-S3 encryption and an HTTPS-only
  bucket policy.
- Lifecycle rule on `meridian/raw/`: `STANDARD` → `STANDARD_IA` (30 d) →
  `GLACIER_IR` (365 d). No expiration of any kind: neither current nor
  non-current versions are ever deleted, per the "raw data is never
  destroyed" hard rule. Note that S3 will not transition an object under
  128 KB, and raw objects are 9-93 KB, so these transitions are expected to
  be a no-op today; they exist for a future larger object profile. See the
  comment in `main.tf` before changing either of them.
- Lifecycle rule on `meridian/manifests/`: non-current versions expire after
  90 d to keep the pointer-churn on `manifests/latest.json` from accumulating
  cost. This is the only rule in the module that deletes anything.
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
    region: "us-east-2"
    prefix: "meridian/"             # must match local.key_prefix in main.tf
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
export AWS_REGION=us-east-2
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
          aws-region: us-east-2
```

No long-lived keys anywhere.

### After the EC2 cutover: removing legacy provider-API secrets

In the pre-EC2-cutover architecture, the weekly-pipeline workflow ran
`meridian run` directly on a GitHub-hosted runner and pulled provider
API keys from GitHub repository secrets. After the cutover (see
`../ec2-cohabit/README.md`), sampling moves to EC2; CI becomes
publish-only and authenticates via the OIDC role above. The legacy
provider-API-key secrets in GitHub are no longer referenced by any
workflow and should be removed — keeping unreferenced secrets is a
passive attack surface (any contributor with workflow-edit access can
exfiltrate them by adding a step that prints them).

Delete via the GitHub UI (repo Settings → Secrets and variables →
Actions) or `gh secret delete`. The provider keys themselves continue
to exist — the EC2 wrapper fetches them from AWS SSM Parameter Store
at sample time, per the cohabit module.

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
- Budget alerts. Use AWS Budgets in the same account. Do not expect the
  lifecycle rules to hold the bill down: every raw object is under S3's
  128 KB transition floor, so the archive stays in STANDARD in practice. At
  this scale that is a few dollars a year against an API spend measured in
  thousands, and the lever that would actually matter is packing weeks into
  fewer, larger objects, not a transition rule.
