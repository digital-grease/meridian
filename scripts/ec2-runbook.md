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
- No SNS message. A clean or warning run is logged only (look for
  `pipeline succeeded` in the wrapper output); SNS fires only when the
  health check fails the week or the pipeline itself fails, with the
  finding in the subject, e.g. `PAGE 2026-W38 anthropic BILLING: 0/1200
  samples`.
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

The instance should start, the wrapper runs and logs `pipeline
succeeded` (a clean run sends no SNS message), and the wrapper stops the
instance (because `WE_OWN_LIFECYCLE=1`).

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
   with `WE_OWN_LIFECYCLE=0`) or accept the gap and record it in
   `data/gaps.jsonl` (per the no-backfill policy), which the site
   renders under `/methodology/#data-gaps` and `/data/coverage/`.

### The run stopped on its cost ceiling

`run-weekly.sh` derives `--max-cost` from that week's roster rather than
using a fixed number: `ceil(estimate x 1.5)`, at least $40 and at most
$100. The log line `pre-flight estimate for <week>: $X; --max-cost
ceiling $Y` records both figures. Two shapes:

- `ABORT: estimated $X exceeds the --max-cost ceiling`: nothing was
  sampled. Only an estimate above $100 can do this, so a config change
  (a new runner, a raised `max_tokens`, a price) moved it. Find that
  change with `uv run python -m meridian.pipeline.cli estimate --week
  <week>` before raising anything.
- `BUDGET CEILING HIT`: actual spend reached the ceiling mid-run and the
  remaining requests were refused. Everything captured before the stop
  is stored. Actual spend at 1.5x the estimate means a model is billing
  far above its cost model; read the run log's per-runner figures.

A deliberate one-off override is `MAX_COST_USD=<n>` in the environment
of a manual re-run. Per-week estimates and the ceiling rule are in
`meridian/BUDGET.md`.

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
4. If capacity does not return the same morning, the week is lost.
   Append a line for it to `data/gaps.jsonl` (`week_id`, `scope`,
   `kind: "lost"`, `runners` for the roster that was due, `reason`,
   `evidence`, `recorded_at`; format in `scripts/check_run_health.py`
   and `site/src/data_coverage.py`). The site build renders it under
   `/methodology/#data-gaps` and `/data/coverage/`; do not hand-edit
   either page. Close the auto-filed issue pointing at that entry. Do not sample it later:
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
`gh workflow run weekly-pipeline.yml -f week=<ISO week>`. To publish
what a killed run did capture, as a disclosed partial week, use
`cli recover-week`; the 2026-W34 procedure below is the worked example.

## Recovery: publish 2026-W34 as a partial week and re-classify W36 to W38 stance

One-off, owner-run. It publishes the samples the killed 2026-W34 run
captured (the owner decision: a disclosed partial week, not an embargo)
and re-measures the stance cells 2026-W36 to W38 published as
unmeasured. No model is re-sampled. The only API calls are to the stance
classifier (Haiku), about 72 of them.

What it uses:

- `scripts/recovery.sh <step>` on the instance, as the `meridian` user,
  with the same environment as `run-weekly.sh` (`/etc/meridian/config.env`,
  repo at `/data/meridian/repo`, `uv`). Each step logs to
  `/data/meridian/logs/recovery-<step>-<timestamp>.log`.
- `cli recover-week` (dry run unless `--write`): builds the W34
  manifest with `partial`, `coverage` and `notes`, the responses
  snapshot, and ONE run_log entry with `recovery: true`, then uploads
  them. It never touches `manifests/latest.json`, never re-uploads raw,
  and refuses before writing anything if the week already has a run_log
  entry or a manifest (local or S3), or if the local run_log differs
  from the S3 copy.
- `scripts/backfill_stance.py classify` (instance, needs the key) and
  `graft` (anywhere, no network), following the 2026-07-24 correction.

**Deadline: 2026-W34 must be on `main` before Monday 2026-10-12 09:00
UTC.** The W34 run_log entry exists only on the instance and in S3 until
the publish workflow commits it. The next weekly run resets the repo to
`origin/main` and uploads its run_log over the S3 copy, which would drop
the entry (bucket versioning keeps the old object, but nothing would
read it).

Preconditions: this change set is merged and pushed to `main`. The
instance checkout is wherever the last weekly run left it and may not
have `scripts/recovery.sh` yet, so the preflight is run as `origin/main`
has it (fetched and read with `git show`); it resets the checkout, and
every later step uses the reset checkout. The preflight refuses a
checkout without `recover-week`. Main must also carry
`data/gaps.jsonl`, which is what turns the W34 health verdict into a
warning instead of a page. Not Monday 08:00 to 14:00 UTC. The instance
is stopped; if it is running, it is specter's: wait.

Run from a laptop with AWS access and `gh`, in `bash` (start `bash`
first if your login shell is fish), from a meridian checkout. Paste one
block at a time and read its output before the next: each block stops
at the first step that does not succeed, and the next block refuses to
start unless the previous one finished.

Block A: setup, read-only checks, start, backstop.

```bash
cd ~/git/digital-grease/meridian   # your meridian checkout
I=i-09453a7a969ca4ea5; R=us-east-2; B=s3://meridian-archive-prod/meridian
STARTED=0; IN_USE=0; OK=none
# Sends one shell command to the instance, waits, prints status and output.
# Returns 0 only on Success; otherwise the command's exit code (or 1).
# The comment is deliberately not the orchestrator's, so the reaper
# ignores this boot: you stop the instance yourself (block D), and the
# backstop below stops it if you cannot.
ssm() {
  local label=$1 cmd=$2 id s code params
  params=$(python3 -c 'import json,sys; print(json.dumps({"executionTimeout":["7200"],"commands":[sys.argv[1]]}))' "$cmd")
  id=$(aws ssm send-command --region "$R" --instance-ids "$I" \
        --document-name AWS-RunShellScript --comment "meridian manual recovery $label" \
        --parameters "$params" --query Command.CommandId --output text) || return 1
  echo "command $id ($label)"
  while :; do
    s=$(aws ssm get-command-invocation --region "$R" --command-id "$id" --instance-id "$I" \
          --query Status --output text 2>/dev/null || echo Pending)
    case "$s" in Pending|InProgress|Delayed) sleep 15 ;; *) break ;; esac
  done
  aws ssm get-command-invocation --region "$R" --command-id "$id" --instance-id "$I" \
      --query '[Status,ResponseCode,StandardOutputContent,StandardErrorContent]' --output text
  [ "$s" = Success ] && return 0
  code=$(aws ssm get-command-invocation --region "$R" --command-id "$id" --instance-id "$I" \
          --query ResponseCode --output text 2>/dev/null)
  case "$code" in ''|*[!0-9]*|0) code=1 ;; esac
  [ "$code" = 75 ] && IN_USE=1
  echo "STOP: $label did not succeed (status $s, exit ${code:-?})."
  return "${code:-1}"
}
# One recovery step from the reset checkout. The preflight instead runs the
# script as origin/main has it, because the checkout may predate it.
step() {
  local c="sudo -u meridian bash /data/meridian/repo/scripts/recovery.sh $1"
  if [ "$1" = preflight ]; then
    c="sudo -u meridian bash -c 'cd /data/meridian/repo && timeout 120 git fetch --quiet origin main && f=\$(mktemp) && git show origin/main:scripts/recovery.sh > \$f && bash \$f preflight; rc=\$?; rm -f \$f; exit \$rc'"
  fi
  ssm "$1" "$c"
}

# 1. Read-only checks. Expect: "stopped"; "stop"; 73; "no manifest"; "same".
STATE=$(aws ec2 describe-instances --region "$R" --instance-ids "$I" \
    --query 'Reservations[].Instances[].State.Name' --output text); echo "$STATE"
SB=$(aws ec2 describe-instance-attribute --region "$R" --instance-id "$I" \
    --attribute instanceInitiatedShutdownBehavior \
    --query InstanceInitiatedShutdownBehavior.Value --output text); echo "$SB"
N=$(aws s3 ls "$B/raw/2026-W34/" --recursive --region "$R" | wc -l); echo "$N"
M=$(aws s3 ls "$B/manifests/2026-W34.json" --region "$R"); echo "${M:-no manifest}"
SAME=0; git fetch origin main && cmp <(aws s3 cp "$B/run_log.jsonl" - --region "$R") \
    <(git show origin/main:data/run_log.jsonl) && SAME=1 && echo same

# 2. Start the instance, only if every check above came out as expected.
#    Not stopped means it is specter's: stop here and come back later.
if [ "$STATE" = stopped ] && [ "$SB" = stop ] && [ "$N" -eq 73 ] && [ -z "$M" ] && [ "$SAME" = 1 ]; then
  aws ec2 start-instances --region "$R" --instance-ids "$I" >/dev/null \
    && aws ec2 wait instance-status-ok --region "$R" --instance-ids "$I" \
    && STARTED=1 && echo "started"
  # Backstop: the instance halts itself (a guest shutdown stops an
  # EBS-backed instance whose shutdown behaviour is "stop", checked above)
  # three hours from now, even if this laptop session dies. The reaper
  # will not cover this boot. To extend: ssm extend "shutdown -c; shutdown -h +120"
  [ "$STARTED" = 1 ] && ssm backstop "shutdown -h +180" && OK=A
else
  echo "STOP: a check above is not as expected; nothing was started."
fi
```

Block B: preflight and plan (no API calls). Read the plan before block C.

```bash
[ "$OK" = A ] || echo "STOP: block A did not finish."
[ "$OK" = A ] && step preflight && step w34-plan && OK=B
```

Block C: build and archive W34, then re-classify W36 to W38 stance.

```bash
[ "$OK" = B ] || echo "STOP: block B did not finish."
[ "$OK" = B ] && step w34-publish && step stance-classify && OK=C
```

Block D: stop the instance. Always run it, whatever happened above.

```bash
if [ "$STARTED" = 1 ] && [ "$IN_USE" = 0 ]; then
  aws ec2 stop-instances --region "$R" --instance-ids "$I" >/dev/null
  aws ec2 wait instance-stopped --region "$R" --instance-ids "$I" && echo "stopped"
elif [ "$STARTED" = 1 ]; then
  # Specter took the box after we started it: leave it running, and cancel
  # the backstop so it does not halt specter's work.
  ssm cancel-backstop "shutdown -c"
  echo "Not stopping: specter is using the instance. Retry the recovery later."
else
  echo "Not stopping: this session did not start the instance."
fi
```

Block E: publish W34 (only after block C printed nothing with STOP).

```bash
[ "$OK" = C ] || echo "STOP: block C did not finish; do not publish."
if [ "$OK" = C ]; then
  prev=$(gh run list --workflow weekly-pipeline.yml --event workflow_dispatch --limit 1 \
           --json databaseId -q '.[0].databaseId // 0')
  gh workflow run weekly-pipeline.yml --ref main -f week=2026-W34
  run=$prev
  for _ in $(seq 1 30); do
    sleep 5
    run=$(gh run list --workflow weekly-pipeline.yml --event workflow_dispatch --limit 1 \
            --json databaseId -q '.[0].databaseId // 0')
    [ "$run" != "$prev" ] && break
  done
  if [ "$run" = "$prev" ]; then
    echo "STOP: the dispatched run did not appear; find it with gh run list."
  else
    gh run watch "$run" --exit-status
  fi
fi
```

Expected output, step by step:

- **1.** `stopped`, `stop`, `73`, `no manifest`, `same`. Step 2 starts
  nothing unless all five hold. If the run_log comparison fails, find
  out why main and S3 disagree before anything appends to either. If the
  shutdown behaviour is not `stop`, the backstop is unsafe (a
  `terminate` would destroy the instance); fix the attribute first.
- **2.** `started`, then the backstop's `Success` with the shutdown
  scheduled. The reaper will not stop or alert on this boot; the
  backstop and block D are the only stops.
- **Block B, preflight.** `GPU memory used: 0 MB`, a `run_log:` line
  (`matches origin/main`, or `origin/main is ahead of the local copy`,
  which is the normal state after a weekly run), `repo at <sha of
  main>`, `preflight ok`. `REFUSED` on the GPU or specter exits 75:
  specter took the box after you started it; run block D, which leaves
  it running and cancels the backstop. `REFUSED: data/run_log.jsonl has
  rows origin/main does not have` means an unpublished row (for example
  a W34 reconstruction from an earlier attempt): publish it before
  anything else.
- **Block B, w34-plan.** `local raw files for 2026-W34: 73`, then per
  model `claude-opus-4-8: 600 samples`, `claude-opus-5: 259 samples`,
  `llama3.2:3b: 750 samples`. `inspect-week` shows opus as `/750`
  because it assumes 25 per pair; read the counts. Then recover-week:

  ```
  anthropic/claude-opus-4-8         600/600  samples  complete complete=30 partial=0 missing=0
  anthropic/claude-opus-5           259/600  samples  partial  complete=12 partial=1 missing=17
      cut short: sci-iq-heritability 19/20
  ollama/llama3.2:3b                750/750  samples  complete complete=30 partial=0 missing=0
  run_log entry: pairs_complete=72 pairs_failed=18 pairs_skipped=0 samples=1609 recovery=true
  captured 2026-08-24T09:... to 2026-08-24T10:...
  s3: no manifest for this week yet; local run_log matches S3
  dry run: nothing written. Re-run with --write to build and archive.
  ```

  The `note:` line ends with `runners is the roster due in 2026-W34`,
  `config_hash e78efdfab25cd47d is the hash logged by the scheduled runs
  either side of 2026-W34` and `host and pid are those of the
  recover-week invocation`. Any `REFUSED` line: stop and read it;
  nothing was written.
- **Block C, w34-publish.** `stance: classified 73 pair(s)` (every
  stored pair: 30 + 13 + 30; about 30 are stance-bearing and reach the
  classifier) with no `STANCE CLASSIFIER DEGRADED` line, `wrote partial
  manifest`, `responses snapshot: 1609 sample(s)`, `run log: appended
  2026-W34 reconstruction (recovery=true, pairs_complete=72,
  pairs_failed=18, actual $...)`, three `s3:` lines with `uploaded 1`
  (the run log line too), `archived 2026-W34`, the two `aws s3 ls`
  lines, and the run_log summary dict with `'runners':
  ['anthropic/claude-opus-4-8', 'anthropic/claude-opus-5',
  'ollama/llama3.2:3b']` and `'config_hash': 'e78efdfab25cd47d'`. If it
  says `REFUSED: the stance classifier failed`, the Anthropic balance or
  key is the problem; nothing was written: fix it, then run `step
  w34-publish && step stance-classify && OK=C` again. If it says
  `ARCHIVE INCOMPLETE`, copy the three files up by hand with `aws s3 cp`
  as the message says; do not re-run w34-publish.
- **Block C, stance-classify.** `2026-W36: 13 unmeasured cell(s)
  re-classified: {...}`, `2026-W37: 19 ...`, `2026-W38: 10 ...`, `wrote
  42 result(s)`, a `results:
  s3://.../corrections/stance-<timestamp>/stance-results.jsonl` line
  (note the path for the local step), and a graft dry run that ends with
  three `diff check passed` lines, a Markdown table of 42 rows with a
  `Response SHA-256` column, and `dry run: nothing written.` `CLASSIFIER
  STILL FAILING` means the key or balance is still bad; fix it and run
  `step stance-classify && OK=C` again, which writes a new timestamped
  directory (successful calls are cached and not repeated).
- **Block D.** `stopped`.
- **Block E.** The watch ends green. The publish commits
  `chore(pipeline): publish 2026-W34 from S3` (manifest, fixture,
  snapshot, run_log), and its health job logs `WARN 2026-W34
  anthropic/claude-opus-5 ACKNOWLEDGED: 259/600 samples`. A `PAGE` means
  `data/gaps.jsonl` was not on main: it does not block the site; push
  the ledger and re-run the health check by hand (`python3
  scripts/check_run_health.py 2026-W34`).

Then, locally on an up-to-date `main` (pull the publish commit first),
in a fresh `bash`:

```bash
cd ~/git/digital-grease/meridian   # your meridian checkout
R=us-east-2
D=YYYY-MM-DD      # the date of the stance-classify step; it becomes both report slugs
RESULTS=s3://...  # the results: path stance-classify printed
mkdir -p /tmp/stance-$D
aws s3 cp "$RESULTS" /tmp/stance-$D/stance-results.jsonl --region "$R"
uv run python scripts/backfill_stance.py graft --results /tmp/stance-$D/stance-results.jsonl --date "$D" --dry-run
uv run python scripts/backfill_stance.py graft --results /tmp/stance-$D/stance-results.jsonl --date "$D" --write
git diff --stat   # exactly six files: data/manifests and site/fixtures for W36, W37, W38
uv run python scripts/check_run_health.py 2026-W37 --manifest data/manifests/2026-W37.json   # no CLASSIFIER-DEAD
```

Then publish the two notices and the ledger lines. The drafts are kept
out of git until this point (the repo is public, and nobody sees a
report before it is published), so they are moved with a plain `mv`:

1. `mv site/content/drafts/stance-classifier-correction.md
   site/content/reports/$D-stance-classifier-correction.md`, replace both
   `YYYY-MM-DD`, and paste the graft's table (with its `Response
   SHA-256` column) over the placeholder table (delete the HTML comment
   above it). The manifests' `corrections` entries already point at
   `/reports/$D-stance-classifier-correction/`.
2. `mv site/content/drafts/w34-partial-week-notice.md
   site/content/reports/$D-w34-partial-week.md` and replace the three
   `YYYY-MM-DD` (publication date, the run_log entry's build date and the
   W34 stance classification date, both the w34-publish date).
3. Append, never edit, one ledger line per corrected week to
   `data/gaps.jsonl`, with `kind: "corrected"` and `scope: "stance"`, so
   the week pages and `/data/coverage/` show the correction next to the
   original `lost` record and stop counting it as an open warning:
   `{"week_id": "2026-W36", "scope": "stance", "kind": "corrected", "reason": "Stance for the 13 unmeasured cells was re-classified on <D> from the published responses and grafted as a versioned correction; only the stance fields changed.", "evidence": ["/reports/<D>-stance-classifier-correction/"], "recorded_at": "<D>"}`
   (W37: 19 cells; W38: 10 cells).
4. In `site/src/templates/data_schema.html`, the `stance_confidence`
   entry says W36 to W38 carry 0.0 on almost every stance-bearing cell
   in their manifests; change it to say they did as first published and
   were corrected on `<D>` (see each manifest's `corrections`). The
   paragraph above the `partial`/`notes`/`coverage`/`corrections` list
   already allows for keys added to an older manifest by a correction;
   check it still reads true.
5. `uv run python -m pytest --tb=short -q`, build the site
   (`uv run python site/src/build.py --manifest site/fixtures/manifest-2026-W40.json --out /tmp/meridian-dist`),
   check `/reports/` lists both, and that `/data/2026-W37/` shows the
   correction, then commit and push.

Never re-dispatch the publish workflow for 2026-W36, W37 or W38 after
this: it copies the S3 manifest, which keeps the stance as first
published, back over the correction. That is the same rule as for every
earlier correction.

Cost: well under $1 in API calls: about 72 Haiku 4.5 calls (30 for W34,
42 for W36 to W38) at $1 and $5 per million input and output tokens,
with inputs of up to a few thousand tokens each (the classified response
is the longest non-refusal sample, and Opus answers run long) and up to
20 output tokens, so roughly $0.10 to $0.30. The instance is billed while
it runs, about $1.21/hour for the g5.2xlarge; the whole sequence takes
well under an hour after boot, so about $1 to $2, and the backstop caps
it at about $4. No Opus or GPT request is made.

## Provider probe

Alert subjects look like:

- `PAGE provider-probe anthropic BILLING (opus-5-5, haiku-4-5)`
- `PAGE provider-probe openai AUTH (gpt-6-astra)`
- `WARN provider-probe openai INCONCLUSIVE (gpt-6-astra)`

`meridian-provider-probe` (infra/terraform/ec2-cohabit/provider_probe.tf)
runs Sunday 12:00 UTC and sends one tiny real request per model with the
same SSM keys the instance uses. A clean Sunday sends nothing. The email
body lists every target with its status, HTTP code and the provider's
message (keys redacted), and says whether that model is in Monday's run.
Credit is account-wide, so a billing failure matters even for a model
that is off this week; the stance classifier runs every week.

Targets follow the runners' `first_week` / `last_week` bounds in
`meridian/config.yaml`, compared against the label of the coming run. A
model whose `last_week` has passed is retired: it is not probed and
cannot page (after 2026-W42, `claude-opus-4-8`, `claude-opus-5` and
`gpt-5.5`). A model before its `first_week` is still probed, so its
access is proven before its first run, and is listed as not in Monday's
run.

What to do, by status:

- **BILLING**: top up before Monday 09:00 UTC. Anthropic credit is
  prepaid and auto-reload is off on purpose, so nothing else will.
  OpenAI: add credit or raise the project's usage limit. Leave at least
  a week of headroom (see `meridian/BUDGET.md` for the per-week
  estimate): the probe spends almost nothing, so a balance that only
  just passes on Sunday can still run dry during Monday's run.
- **AUTH**: the key was rejected, or its SSM parameter is missing. Put a
  working key back with the `aws ssm put-parameter` command from
  `terraform output set_anthropic_key_command` (or the OpenAI one).
- **MODEL-GONE**: the provider no longer serves that model id, or the
  key's project has lost access to it (OpenAI reports that as a 403 with
  `model_not_found`; the key itself is fine). Update
  `runners:` (or `stance:`) in `meridian/config.yaml` and
  `provider_probe_targets` in `infra/terraform/ec2-cohabit/variables.tf`
  in the same change, then apply. A replaced model is a new series:
  give the old entry a `last_week` and the new one a `first_week` (never
  delete the old entry), and record the change as a dated notice under
  `site/content/reports/` (see `2026-10-06-roster-succession.md`).
- **INCONCLUSIVE**: a timeout, a 5xx that survived the one retry, or an
  unfamiliar 400. Usually transient. A 400 that repeats every Sunday on
  one model, with the others OK, more likely means the 16-token probe
  request does not suit that model (for example one that always thinks,
  such as `claude-opus-5-5`) than that Monday will fail; the run's own
  requests carry an 8192 cap. Re-run the probe and only act if it
  repeats. The exception is "could not read SSM parameter": nothing was
  sent to the provider, so check the `meridian-provider-probe` role's
  `ssm:GetParameter` (and `kms:Decrypt`) grant. When every key read
  fails the probe has checked nothing, and the email is a `PAGE`.

Re-run by hand after fixing anything (the result prints per target; it
emails only if something is still not OK):

```bash
aws lambda invoke \
    --function-name meridian-provider-probe \
    --region us-east-2 \
    /tmp/meridian-provider-probe.json
cat /tmp/meridian-provider-probe.json
```

`meridian-provider-probe-errors` firing means the probe itself did not
finish (bad target list, timeout, or an SNS publish that failed), so
this Sunday's check did not happen. Read
`aws logs tail /aws/lambda/meridian-provider-probe --since 24h --region us-east-2`
and run it by hand.

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
