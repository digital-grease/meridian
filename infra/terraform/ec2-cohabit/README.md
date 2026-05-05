# `infra/terraform/ec2-cohabit` — meridian on specter's GPU instance

Provisions the AWS resources that let the Meridian weekly pipeline run
on the existing `g5.2xlarge` provisioned by `digital-grease/specter`
(`../../../specter/infra/main.tf`). Meridian **never owns the instance** —
this module attaches a separate EBS volume, extends specter's IAM role
with meridian-specific policies, and provisions a Lambda + EventBridge
Scheduler + SNS topic to drive the weekly run.

The companion modules are:
- `../s3/` — provisions the durable raw-sample archive bucket. Apply this
  first; pass its name into `archive_bucket_name` here.
- `../../../specter/infra/` — provisions the EC2 instance, its IAM role
  (`SpecterInstanceRole`), and instance profile (`SpecterInstanceProfile`).
  Must already exist when this module is applied.

## What you get

- **Dedicated EBS volume** (`meridian-data`, gp3, 30 GB by default,
  `prevent_destroy=true`) attached to specter's instance at `/dev/sdg`.
  Mounted at `/data/meridian` by the Phase 3 on-instance bootstrap;
  parallel to specter's own `/data` (which lives on `/dev/xvdf`).
- **Meridian policies attached to specter's IAM role**:
  - `meridian-instance-archive-write` — S3 read/write on the archive bucket.
  - `meridian-instance-secrets-read` — `ssm:GetParameter` on the API-key paths.
  - `meridian-instance-ops` — `sns:Publish` on the alerts topic and
    `ec2:StopInstances` on instances tagged `Name=specter` (the wrapper
    self-stops at end of run when it owns lifecycle).
- **SSM Parameter Store** entries for `ANTHROPIC_API_KEY` and
  `OPENAI_API_KEY` (`SecureString`). Terraform creates the parameter
  shells with placeholders; you populate the real values via aws-cli
  (see "After apply" below).
- **SNS topic** `meridian-pipeline-alerts` with an email subscription.
  Receives deferral and failure notifications.
- **Lambda** `meridian-orchestrator` (Python 3.12, ~150 LoC). Fires on
  schedule, starts the instance if stopped, waits for SSM-reachable,
  fires the wrapper script via SSM, exits. Does **not** wait for the
  pipeline to finish (Lambda's 15-min hard timeout cannot sit through a
  ~30-90 min run); the wrapper handles its own outcome reporting and
  self-shutdown.
- **EventBridge Scheduler** schedule `meridian-weekly` at
  `cron(0 4 ? * MON *)` in `America/Chicago` by default. Timezone-aware
  via the AWS-native `schedule_expression_timezone` field — no manual
  DST math.

## Apply

```bash
cd infra/terraform/ec2-cohabit

terraform init
terraform plan \
  -var alert_email="you@example.com" \
  -var archive_bucket_name="<bucket from ../s3/>" \
  -var instance_id="i-0123456789abcdef0"   # optional; tag-lookup is the fallback
terraform apply <same vars>
```

## After apply (manual steps)

The two manual steps are: confirm the SNS subscription, and populate
the SSM parameters with the real API keys.

### 1. Confirm SNS subscription

After the first apply, AWS sends a confirmation email to the address
in `var.alert_email`. Click the link before the first run — if the
subscription is unconfirmed, alerts are silently dropped.

### 2. Populate the SSM parameters

Terraform has created the parameter shells with placeholder values.
Set the real values via aws-cli; `lifecycle.ignore_changes = [value]`
on the resource means subsequent `terraform apply` won't drift them.

```bash
# (terraform output gives you these commands verbatim)
aws ssm put-parameter --region us-east-2 --type SecureString --overwrite \
    --name /meridian/anthropic-api-key \
    --value '<ANTHROPIC_API_KEY>'

aws ssm put-parameter --region us-east-2 --type SecureString --overwrite \
    --name /meridian/openai-api-key \
    --value '<OPENAI_API_KEY>'
```

After this, both keys live encrypted in SSM Parameter Store under the
AWS-managed `aws/ssm` KMS key. The instance profile grants the
GetParameter permission; the wrapper script sets
`MERIDIAN_SECRETS_SSM=1` and the corresponding `*_PATH` env vars,
and the meridian CLI's `resolve_ssm_secrets()` helper fetches them at
runtime.

## Test the orchestrator manually

Before waiting for Monday 04:00, verify the Lambda end-to-end:

```bash
aws lambda invoke \
    --function-name meridian-orchestrator \
    --region us-east-2 \
    /tmp/meridian-orch.json

cat /tmp/meridian-orch.json
# Expect: {"status": "dispatched", "instance_id": "i-...", "ssm_command_id": "...", "we_own_lifecycle": true}
```

If the instance is currently running (specter is using it), expect
`{"status": "deferred", ...}` and an SNS deferral email.

To watch the wrapper run after dispatch:

```bash
INSTANCE=$(terraform output -raw instance_id)
aws ssm start-session --target $INSTANCE --region us-east-2
# inside the session
sudo journalctl -u meridian -f          # if Phase 3 set up the systemd service
# or
tail -f /data/meridian/logs/run-weekly.log
```

## What this module does NOT do

- **Provision the instance.** Specter does that.
- **Install ollama, the meridian repo, or the wrapper script on the
  instance.** That's Phase 3 of the main plan (`.devloop/plan.md`).
  The wrapper script path is referenced by `var.wrapper_script_path`
  (default `/data/meridian/repo/scripts/run-weekly.sh`); the file
  needs to actually exist there before the first run.
- **Create the GitHub Actions OIDC role for CI.** That lives in `../s3/`
  with `enable_github_oidc_role=true`.

## Rollout checklist

End-to-end cutover from "ollama runs manually on `mflowers`" to "EC2
runs everything weekly, CI publishes." Follow in order — each step
assumes the previous succeeded.

1. **Review the Phase 1–4 diff.** Code (`meridian/`), Terraform
   (`infra/terraform/ec2-cohabit/`), scripts (`scripts/`), and the
   rewritten workflow (`.github/workflows/weekly-pipeline.yml`).
2. **Verify the S3 bucket exists.** `terraform plan` in
   `../s3/`; the bucket should already be applied. If it isn't, apply
   it first (`terraform apply -var bucket_name=<your-bucket>`).
3. **Enable the GitHub OIDC role** in the s3 module if it isn't
   already. Re-apply with `-var enable_github_oidc_role=true
   -var github_oidc_provider_arn=... -var github_repository=digital-grease/meridian`.
   Capture the resulting role ARN from `terraform output github_oidc_role_arn`.
4. **Apply the cohabit module.** From `infra/terraform/ec2-cohabit/`:
   ```bash
   terraform apply \
       -var alert_email="you@example.com" \
       -var archive_bucket_name="<bucket from step 2>"
   ```
5. **Confirm the SNS email subscription.** Click the link AWS sent.
6. **Populate the two SSM parameters** with real API keys via the
   `aws ssm put-parameter` commands in the `terraform output`.
7. **Wire `storage.s3:` in `meridian/config.yaml`.** Uncomment the
   block and fill in `bucket: "<your-bucket>"`. Commit.
8. **Add GitHub repository variables and secrets**:
   - `AWS_ROLE_TO_ASSUME` (secret) — the OIDC role ARN from step 3.
   - `S3_BUCKET` (variable) — same bucket name as step 2.
   - `S3_PREFIX` (variable, optional) — defaults to `meridian/`.
9. **Walk through `scripts/ec2-runbook.md`** once via SSM session:
   bootstrap, repo clone, `ollama pull llama3.2:3b`, populate
   `/etc/meridian/config.env`, smoke-test the wrapper, end-to-end
   Lambda test.
10. **Wait for the first scheduled fire** (Mon 04:00 America/Chicago).
    Confirm CI's Mon 13:00 UTC publish step picks up the artifact and
    the dashboard renders.
11. **Delete the now-unused GitHub Secrets** (manually — only the user
    can do this, the GitHub UI doesn't expose a Terraform path):
    - `ANTHROPIC_API_KEY`
    - `OPENAI_API_KEY`

    These were used by the old `weekly-pipeline.yml` to sample on
    GitHub-hosted runners. After the cutover, sampling lives on EC2
    with keys in SSM Parameter Store; CI authenticates via OIDC and
    has no need for them. Leaving them in repo settings is a passive
    attack surface (anyone with `secrets:read` can dump them in a
    workflow).

    To delete: GitHub repo → Settings → Secrets and variables →
    Actions → click each secret → Remove. Or via gh-cli:
    `gh secret delete ANTHROPIC_API_KEY` and
    `gh secret delete OPENAI_API_KEY`.

12. **Add the Phase 5 roster expansion** once on-instance setup is
    confirmed: pull `qwen2.5:7b`, `gemma2:9b` (or `gemma3:9b`),
    `phi3.5:3.8b`, `mistral-nemo:12b`, `deepseek-llm:7b`; capture
    each digest; commit them to `config.yaml`. (See `.devloop/plan.md`
    Phase 5.)
13. **After the first complete weekly run ships**, write the dated
    methodology disclosure (Phase 6).

## Cost

Per the spike (`.devloop/spikes/ollama-ci-automation.md`):

- EC2 g5.2xlarge on-demand at ~30-60 min/week: **~$3-5/mo**
- gp3 EBS, 30 GB always: **~$2.40/mo**
- Lambda + Scheduler + SNS + CloudWatch Logs: **<$0.50/mo**
- **Total: ~$6-8/mo marginal** above whatever specter is already costing.

## Tear-down notes

`prevent_destroy = true` on `aws_ebs_volume.meridian_data` is the
project's "raw data is never destroyed" rule at the IaC layer. To
intentionally retire:

```bash
terraform state rm aws_ebs_volume.meridian_data
# then delete the volume manually after auditing what it holds
```

The EBS volume contains ollama model files (regenerable) and a copy of
`/data/meridian/run_log.jsonl` and the in-flight raw samples that
haven't yet been uploaded to S3 (recoverable from S3 if upload
succeeded).
