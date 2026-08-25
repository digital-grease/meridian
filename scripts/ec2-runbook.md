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

## Recovery: instance won't start (capacity)

Alert subject: `[meridian] capacity unavailable — instance did not start`.

`StartInstances` failed with `InsufficientInstanceCapacity`: AWS has no
free capacity for this instance type in its AZ right now. Nothing is
broken on our side, and there is nothing to fix in the pipeline.

This is not relocatable. We cohabit specter's instance and a stopped
instance is pinned to its subnet, so another AZ or instance type would
mean a different box than the one the design shares. Waiting is the
only lever.

1. Confirm the cause:
   `aws logs tail /aws/lambda/meridian-orchestrator --since 1h --region us-east-2`
2. Check whether capacity has returned by retrying the fire. Use the
   same flags as the smoke test above — `--cli-read-timeout 600` is
   required for the reason documented there, and omitting it makes the
   CLI time out and re-invoke, firing the function twice:

   ```bash
   aws lambda invoke \
       --function-name meridian-orchestrator \
       --cli-read-timeout 600 --cli-connect-timeout 10 \
       --region us-east-2 \
       /tmp/meridian-orch.json
   cat /tmp/meridian-orch.json
   ```

   `{"status": "dispatched", ...}` means capacity came back and the run
   is under way. A `CapacityUnavailable` error means keep waiting. Pass
   no `--payload`: the handler logs the event but reads nothing from it,
   and on AWS CLI v2 a raw JSON payload needs
   `--cli-binary-format raw-in-base64-out` or the call fails outright.
3. Watch the clock. The publish workflow reads S3 at 13:00 UTC and the
   run takes 30-90 minutes, so a start after roughly 11:30 UTC will not
   publish the same day. It is still worth running: re-trigger the
   publish afterwards with `gh workflow run weekly-pipeline.yml -f week=<ISO week>`.
4. If capacity does not return the same morning, the week is lost. Add
   it to `#data-gaps` in `site/src/templates/methodology.html` and close
   the auto-filed issue pointing at that entry. Do not sample it later:
   a sample taken Thursday is not a Monday sample, and backdating one
   would corrupt exactly the signal this project measures.

Capacity outages that recur week over week are worth escalating: two
consecutive losses (2026-W30, 2026-W31) is already a meaningful hole in
the longitudinal record. Options at that point are an On-Demand Capacity
Reservation for the Monday window (bills continuously, so it conflicts
with the infra budget target) or negotiating a different cohabitation
host with specter.

## Recovery: instance left running after a run

Alert subjects:

- `[meridian] reaper stopped an instance the weekly run left running`
- `[meridian] ATTENTION: instance still running, reaper could not verify it is idle`
- `[meridian] ATTENTION: instance running after meridian finished, but it is busy`

`scripts/run-weekly.sh` stops the instance itself on every exit path it
can reach. The qualifier is the point: the stop is a function call, not
a trap, and no trap survives `SIGKILL` anyway. Anything that kills the
wrapper outright skips it, and a g5.2xlarge left running costs roughly
$1.21/hour against an infra budget of about $45/month.

`meridian-reaper` (infra/terraform/ec2-cohabit/reaper.tf) runs hourly
and cleans up after exactly that. It stops the instance only when
meridian started the current boot and meridian's own run has already
reached a terminal status, and it re-checks that the box is idle before
acting. A boot that meridian did not start is specter's and is never
touched.

**Every reaper alert is a bug report.** The reaper firing means the
wrapper did not stop its own instance, and that cause is still there
whether or not the box got stopped. Do not close the alert on the stop
alone.

1. Find the run it cleaned up after:

   ```bash
   aws ssm list-command-invocations \
       --region us-east-2 --details --max-items 5 \
       --query 'CommandInvocations[].{Cmd:CommandId,Status:Status,Code:ResponseCode,Elapsed:ExecutionElapsedTime,Req:RequestedDateTime}' \
       --output table
   ```

2. Read the status. `TimedOut` with `ResponseCode 137` and an
   `ExecutionElapsedTime` suspiciously close to a round number is SSM
   killing the wrapper at the `executionTimeout` ceiling, which is what
   happened in 2026-W34 at exactly `PT1H0.004S`. Raise
   `ssm_execution_timeout_seconds` and re-apply. Any other terminal
   status means the wrapper died some other way; go to
   `/data/meridian/logs/run-weekly.log` for the cause.

3. If the alert says the reaper *could not verify* the box is idle, it
   deliberately did not stop it and the instance is still billing.
   Confirm nothing is running, then stop it by hand:

   ```bash
   aws ec2 stop-instances --instance-ids <id> --region us-east-2
   ```

4. If the alert says the box is *busy*, that is most likely specter work
   started after meridian finished. Nothing is wrong except meridian's
   failed self-stop. Stop it when the box is free.

A run killed part-way writes no manifest, so the publish workflow will
404 for that week. Once the cause is fixed and a run has completed,
publish it with
`gh workflow run weekly-pipeline.yml -f week=<ISO week>`.

## When a run finishes after 13:00 UTC

The publish workflow reads S3 on a fixed schedule and does not come back
later. A run that finishes after it has already gone red leaves a
complete, healthy manifest sitting in S3 that nothing will ever commit,
and the dashboard silently keeps serving the previous week.

This is not hypothetical and it is easy to miss: 2026-W33 sampled
successfully at 16:31 UTC after capacity retries pushed the start to
16:04, three hours after the 13:00 publish had already 404'd and filed
its issue. The manifest sat unpublished for eight days while the failure
looked identical to a week that produced no data at all.

Before assuming a red publish means a lost week, check whether the data
exists:

```bash
aws s3 ls s3://meridian-archive-prod/meridian/manifests/ --region us-east-2 | tail -5
```

If the week's manifest is there, nothing needs re-sampling. Publish it:

```bash
gh workflow run weekly-pipeline.yml --ref main -f week=<ISO week>
```

The `health` job may still go red on a data-quality finding. That is by
design and does not block the site: the site build gates on the
`publish` job's `artifacts_committed` output, not on the workflow's
conclusion.
