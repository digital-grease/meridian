# EC2 cohabit — on-instance runbook

The Terraform module at `infra/terraform/ec2-cohabit/` provisions the
AWS-side resources (EBS volume, IAM, Lambda, Scheduler, SNS, SSM
parameter shells). Once `terraform apply` is clean, this runbook
covers the **on-instance** setup that has to happen interactively over
an SSM session before the first weekly run can fire.

## Prerequisites

- `terraform apply` succeeded in `infra/terraform/ec2-cohabit/`.
- SNS subscription confirmed (you clicked the email link).
- SSM parameters populated with real API keys:

  ```bash
  aws ssm put-parameter --region us-east-2 --type SecureString --overwrite \
      --name /meridian/anthropic-api-key --value '<ANTHROPIC_API_KEY>'
  aws ssm put-parameter --region us-east-2 --type SecureString --overwrite \
      --name /meridian/openai-api-key --value '<OPENAI_API_KEY>'
  ```

  (Both `terraform output set_anthropic_key_command` and
  `set_openai_key_command` print these verbatim with the right path.)

- Specter's instance is currently **stopped** (so we don't collide
  with their experiment during setup).

## Step 1 — Start the instance and open an SSM session

```bash
INSTANCE=$(terraform -chdir=infra/terraform/ec2-cohabit output -raw instance_id)
aws ec2 start-instances --instance-ids "$INSTANCE" --region us-east-2
aws ec2 wait instance-status-ok --instance-ids "$INSTANCE" --region us-east-2
aws ssm start-session --target "$INSTANCE" --region us-east-2
```

You're now on the instance as the SSM-default user (`ssm-user`). All
the steps below assume you're elevating with `sudo` where needed; the
bootstrap script must run as root.

## Step 2 — Run the bootstrap script

The repo isn't on the instance yet, so fetch the bootstrap script
directly from GitHub. The script is idempotent — re-running is safe.

```bash
curl -fsSL https://raw.githubusercontent.com/digital-grease/meridian/main/scripts/ec2-bootstrap.sh \
  | sudo bash
```

What that does (per `scripts/ec2-bootstrap.sh`):

1. Finds the meridian EBS volume (probes `/dev/sdg`, `/dev/xvdg`,
   `/dev/nvme*n1` — skips specter's `/dev/xvdf`).
2. Formats it ext4 if blank, mounts at `/data/meridian`, persists via
   fstab UUID entry.
3. Creates a `meridian` system user owning `/data/meridian`.
4. Installs ollama, configures `OLLAMA_MODELS=/data/meridian/ollama-models`
   so model files live on the dedicated volume, enables the service.
5. Installs `uv` system-wide.
6. Drops `/etc/meridian/config.env` template (placeholders need real
   values — Step 3).

Tail of the log:

```bash
tail -20 /var/log/meridian-bootstrap.log
```

## Step 3 — Populate `/etc/meridian/config.env`

```bash
# From your laptop, get the exact SNS ARN:
terraform -chdir=infra/terraform/ec2-cohabit output -raw alerts_topic_arn

# In the SSM session:
sudo nano /etc/meridian/config.env
```

Two fields need values:

- `SNS_TOPIC_ARN` — paste the ARN from `terraform output -raw alerts_topic_arn`.
- `MERIDIAN_S3_BUCKET` — paste your archive bucket name (matches
  `bucket_name` in `infra/terraform/s3/terraform.tfvars` and
  `storage.s3.bucket` in `meridian/config.yaml`).

The other defaults (region, prefix, SSM secret paths) match the
Terraform modules' defaults; only override if you customised them.

## Step 4 — Clone the meridian repo

```bash
sudo -u meridian git clone https://github.com/digital-grease/meridian.git /data/meridian/repo
sudo -u meridian bash -c 'cd /data/meridian/repo && uv sync --group changepoint --group analysis-heavy'
```

`--group changepoint` adds `ruptures` for change-point detection and
`--group analysis-heavy` adds sentence-transformers + numpy (~5 GB with
pytorch) for embedding-centroid drift. Both run during the manifest
build, so the weekly wrapper (`scripts/run-weekly.sh`) installs the same
two groups — keep this command in sync with it. boto3 is in the main
dependency block, so plain `uv sync` already provides it. `analysis-heavy`
is required while `embedding.enabled: true` in `config.yaml` (the current
setting); drop it only if you turn embeddings off.

The wrapper script (`scripts/run-weekly.sh`) is now at the path
`infra/terraform/ec2-cohabit/variables.tf` references as
`var.wrapper_script_path` (`/data/meridian/repo/scripts/run-weekly.sh`).

## Step 5 — Pull the open-weight roster and pin digests

For Phase 3 we install only `llama3.2:3b` (the existing pin) to keep
the smoke surface small. Phase 5 expands the roster to 6 models.

```bash
sudo -u meridian ollama pull llama3.2:3b

# Capture the digest. This MUST match what's pinned in meridian/config.yaml:
sudo -u meridian ollama list
# or, more precisely:
curl -s http://localhost:11434/api/tags | python3 -c "
import json, sys
for m in json.load(sys.stdin)['models']:
    if m['name'] == 'llama3.2:3b':
        print(m['digest'])
"
```

Compare the printed digest against the `digest:` field in
`meridian/config.yaml`. If they match, the pipeline's pre-flight digest
check will pass. If they don't, **stop**: either the upstream model has
been re-pushed, or you pulled a different tag — investigate before
running the pipeline.

## Step 6 — Smoke test the wrapper (manually, no Lambda)

This bypasses the Lambda entirely; we run the wrapper directly to
shake out config issues before the first scheduled fire.

```bash
# From your laptop:
SNS_ARN=$(terraform -chdir=infra/terraform/ec2-cohabit output -raw alerts_topic_arn)

# In the SSM session:
sudo -i -u meridian env \
  WE_OWN_LIFECYCLE=0 \
  SNS_TOPIC_ARN="$SNS_ARN" \
  bash /data/meridian/repo/scripts/run-weekly.sh
```

`WE_OWN_LIFECYCLE=0` — the smoke test should **not** stop the
instance afterward; we still want to inspect things.

Expected outcome:
- Pre-flight passes (no specter processes; GPU idle).
- Pipeline runs against the previous ISO week.
- Raw + manifest + snapshot upload to S3.
- SNS publishes a "weekly run succeeded" message.
- Wrapper exits 0; you're still in the SSM session.

If the run completes cleanly, stop the instance from your laptop:

```bash
aws ec2 stop-instances --instance-ids "$INSTANCE" --region us-east-2
```

## Step 7 — End-to-end test of the orchestrator Lambda

Last step — confirm the scheduled path works end-to-end. With the
instance stopped:

```bash
# IMPORTANT: --cli-read-timeout 600 is required.
# The Lambda's _wait_for_ready blocks for up to ~10 min while the
# instance boots. Without this flag, AWS CLI's default 60s read
# timeout fires; the CLI retries; AWS Lambda treats the retry as a
# fresh invocation and fires the function a second time. The second
# invocation finds the instance already running and takes the
# deferral path (which is harmless but generates a noisy SNS email
# and a misleading CloudTrail trail).
aws lambda invoke \
    --function-name meridian-orchestrator \
    --cli-read-timeout 600 --cli-connect-timeout 10 \
    --region us-east-2 \
    /tmp/meridian-orch.json

cat /tmp/meridian-orch.json
# Expect: {"status": "dispatched", ..., "we_own_lifecycle": true}
```

Watch CloudWatch Logs for the Lambda:

```bash
aws logs tail /aws/lambda/meridian-orchestrator --region us-east-2 --follow
```

The instance should start, the wrapper runs, SNS publishes "weekly
run succeeded," and the wrapper stops the instance (because
`WE_OWN_LIFECYCLE=1`).

If everything works, the EventBridge Scheduler will fire the same
flow automatically every Monday at 04:00 America/Chicago.

## Recovery: pipeline fails on the instance

The wrapper script has tee'd full output to
`/data/meridian/logs/run-weekly-<timestamp>.log` and to journald (via
SSM Session). If a run fails:

1. Open an SSM session to the instance (it may have been left running
   if the wrapper crashed before reaching the self-stop step).
2. `tail -200 /data/meridian/logs/run-weekly.log` for the full output.
3. `tail -1 /data/meridian/repo/data/run_log.jsonl` for the structured
   failure entry.
4. Decide whether to re-run by hand (manual `run-weekly.sh` invocation
   with `WE_OWN_LIFECYCLE=0`) or accept the gap and document it on
   the methodology page (per the no-backfill policy).
