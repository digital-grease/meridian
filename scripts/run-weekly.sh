#!/usr/bin/env bash
# Meridian weekly pipeline wrapper. Invoked on the EC2 cohabit
# instance by the orchestrator Lambda via SSM SendCommand:
#
#   WE_OWN_LIFECYCLE=1 SNS_TOPIC_ARN=arn:... /data/meridian/repo/scripts/run-weekly.sh
#
# Responsibilities:
#   1. Pre-flight contention check (GPU + specter process).
#      Defer with SNS alert if either signal trips.
#   2. Fetch provider API keys from SSM Parameter Store at sample
#      time (handled by `meridian.secrets.resolve_ssm_secrets`).
#   3. Compute the previous ISO week (because at Mon 04:00 CDT we've
#      just rolled into the new week) and run the pipeline against it.
#   4. Publish a structured outcome to SNS regardless of success/failure.
#   5. If WE_OWN_LIFECYCLE=1, stop the EC2 instance after we're done.
#
# Exit codes:
#   0  clean run (or deferred without contention being a real problem)
#   1  pipeline run failed
#   2  pre-flight contention detected — deferred
#   3  config or environment issue
#
# Logs are tee'd to /data/meridian/logs/run-weekly.log for forensic
# recovery if the SSM session output is lost.

set -uo pipefail

# Source the on-instance config (SNS topic, region, SSM secret paths).
if [ -f /etc/meridian/config.env ]; then
  # shellcheck disable=SC1091
  set -a
  . /etc/meridian/config.env
  set +a
fi

LOG_DIR="${LOG_DIR:-/data/meridian/logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run-weekly-$(date -u +%Y%m%dT%H%M%SZ).log"
ln -sf "$LOG_FILE" "$LOG_DIR/run-weekly.log"
exec > >(tee -a "$LOG_FILE") 2>&1

REPO_DIR="${REPO_DIR:-/data/meridian/repo}"
WE_OWN_LIFECYCLE="${WE_OWN_LIFECYCLE:-0}"
SNS_TOPIC_ARN="${SNS_TOPIC_ARN:-}"
AWS_REGION="${AWS_DEFAULT_REGION:-us-east-2}"
GPU_MEMORY_THRESHOLD_MB="${GPU_MEMORY_THRESHOLD_MB:-500}"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# IMDSv2 token + instance id, for self-stop.
IMDS_TOKEN=$(curl -sS -X PUT \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
  http://169.254.169.254/latest/api/token || true)
INSTANCE_ID=$(curl -sS \
  -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
  http://169.254.169.254/latest/meta-data/instance-id || true)

publish() {
  # SNS alerts are best-effort. CloudWatch / journald is the truth.
  local subject="$1" body="$2"
  if [ -z "$SNS_TOPIC_ARN" ] || [ "$SNS_TOPIC_ARN" = "REPLACE-WITH-SNS-TOPIC-ARN" ]; then
    log "(no SNS_TOPIC_ARN set; skipping publish: $subject)"
    return 0
  fi
  aws --region "$AWS_REGION" sns publish \
    --topic-arn "$SNS_TOPIC_ARN" \
    --subject "[meridian] $subject" \
    --message "$body" >/dev/null 2>&1 || log "SNS publish failed for: $subject"
}

self_stop_if_needed() {
  if [ "$WE_OWN_LIFECYCLE" = "1" ]; then
    if [ -z "$INSTANCE_ID" ]; then
      log "WE_OWN_LIFECYCLE=1 but couldn't resolve INSTANCE_ID via IMDSv2; not stopping."
      return
    fi
    log "stopping instance $INSTANCE_ID (we own lifecycle)"
    aws --region "$AWS_REGION" ec2 stop-instances --instance-ids "$INSTANCE_ID" >/dev/null \
      || log "stop-instances failed; instance will continue running until manual intervention"
  else
    log "leaving instance running — we did not start it (WE_OWN_LIFECYCLE=$WE_OWN_LIFECYCLE)"
  fi
}

# ----------------------- 1. Pre-flight ---------------------------------
log "pre-flight: checking GPU memory"
GPU_USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
  | awk '{print $1}' | head -1 || echo 0)
log "GPU memory used: ${GPU_USED} MB (threshold ${GPU_MEMORY_THRESHOLD_MB})"
if [ "$GPU_USED" -gt "$GPU_MEMORY_THRESHOLD_MB" ]; then
  publish "deferred — GPU busy at start" "GPU memory in use: ${GPU_USED} MB. Specter or another workload is on the instance. Skipping this week per the no-backfill policy."
  # Don't self-stop; if specter is using the GPU, we shouldn't kill the
  # instance out from under it.
  exit 2
fi

log "pre-flight: scanning for specter processes"
if pgrep -af 'specter' >/dev/null 2>&1; then
  PROC_DETAIL=$(pgrep -af 'specter' | head -5)
  publish "deferred — specter process detected" "pgrep matched:\n${PROC_DETAIL}\nSkipping this week per the no-backfill policy."
  exit 2
fi
log "pre-flight: clear"

# ----------------------- 2. Repo + deps --------------------------------
if [ ! -d "$REPO_DIR/.git" ]; then
  publish "FAILED — repo not present" "Expected meridian repo at $REPO_DIR but no .git directory found. Run the on-instance bootstrap and clone the repo before triggering."
  exit 3
fi

log "git pull origin main"
cd "$REPO_DIR"
git fetch --quiet origin main || log "WARN: git fetch failed; running with current code"
git -c advice.detachedHead=false reset --hard origin/main || log "WARN: git reset failed"

log "uv sync"
uv sync --frozen >/dev/null || uv sync >/dev/null

# ----------------------- 3. Run pipeline -------------------------------
WEEK=$(date -u --date='yesterday' +'%G-W%V')
log "running pipeline for ISO week $WEEK"

# meridian.secrets.resolve_ssm_secrets() activates because
# MERIDIAN_SECRETS_SSM=1 was set in /etc/meridian/config.env.
PIPELINE_START_EPOCH=$(date -u +%s)
set +e
uv run python -m meridian.pipeline.cli run --week "$WEEK" --yes
RUN_RC=$?
set -e
PIPELINE_END_EPOCH=$(date -u +%s)
ELAPSED=$((PIPELINE_END_EPOCH - PIPELINE_START_EPOCH))

# ----------------------- 4. Report -------------------------------------
RUN_LOG_TAIL=$(tail -1 "$REPO_DIR/data/run_log.jsonl" 2>/dev/null || echo "(no run_log entry)")

if [ "$RUN_RC" -eq 0 ]; then
  publish "weekly run succeeded ($WEEK)" \
    "Wall-clock: ${ELAPSED}s\nWE_OWN_LIFECYCLE=$WE_OWN_LIFECYCLE\n\nRun-log entry:\n${RUN_LOG_TAIL}"
  log "pipeline succeeded in ${ELAPSED}s"
else
  publish "weekly run FAILED ($WEEK, rc=$RUN_RC)" \
    "Wall-clock: ${ELAPSED}s\nWE_OWN_LIFECYCLE=$WE_OWN_LIFECYCLE\n\nLog tail:\n$(tail -80 "$LOG_FILE")\n\nRun-log entry (if any):\n${RUN_LOG_TAIL}"
  log "pipeline FAILED with rc=$RUN_RC in ${ELAPSED}s"
fi

# ----------------------- 5. Self-stop ----------------------------------
self_stop_if_needed

exit "$RUN_RC"
