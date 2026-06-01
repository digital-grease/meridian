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

# Wait for ollama to be reachable. systemd starts the unit at boot but
# the daemon needs a few seconds to bind 11434, and longer if the
# models dir has to be scanned. 180s cap covers cold-boot with a
# populated /data/meridian/ollama-models; typical wait is 5-30s.
log "waiting for ollama to be reachable"
for _ in $(seq 1 90); do
  if curl -fsS --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
    log "ollama reachable"
    break
  fi
  sleep 2
done
if ! curl -fsS --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
  publish "FAILED — ollama not reachable" "ollama did not respond on http://localhost:11434/api/tags within 180s. Check 'systemctl status ollama' on the instance."
  exit 3
fi

# ----------------------- 2. Repo + deps --------------------------------
if [ ! -d "$REPO_DIR/.git" ]; then
  publish "FAILED — repo not present" "Expected meridian repo at $REPO_DIR but no .git directory found. Run the on-instance bootstrap and clone the repo before triggering."
  exit 3
fi

log "git pull origin main"
cd "$REPO_DIR"
git fetch --quiet origin main || log "WARN: git fetch failed; running with current code"
git -c advice.detachedHead=false reset --hard origin/main || log "WARN: git reset failed"

# The manifest build runs the heavy analyses on this host (embedding-
# centroid drift + PELT change-point detection), so their optional
# dependency groups must be installed here. Plain `uv sync` installs only
# the default group; without --group the analysis-heavy (sentence-
# transformers + numpy) and changepoint (ruptures) deps are absent, the
# lazy imports fail, and embedding_centroid_shift / change_points come out
# silently empty on every record. CI never installs these — it only
# publishes the manifest this host already built. The GPU on the cohabit
# g5.2xlarge makes the embedding pass cheap; deps cache on the persistent
# EBS volume so the cost is paid once.
# The non-frozen fallback mirrors .github/workflows/weekly-build.yml: a fresh
# resolve must still honour the 7-day dependency cooldown (renovate.json /
# COOLDOWNS.md), and this host produces the data of record, so an un-cooled
# resolve here is worse than in CI. --exclude-newer "7 days" needs uv>=0.9.17
# (what ec2-bootstrap installs); the final bare resolve is a last-ditch guard
# if an older uv on a long-lived instance can't parse the relative duration.
log "uv sync (+ analysis-heavy, changepoint)"
uv sync --frozen --group analysis-heavy --group changepoint >/dev/null \
  || uv sync --exclude-newer "7 days" --group analysis-heavy --group changepoint >/dev/null \
  || uv sync --group analysis-heavy --group changepoint >/dev/null

# ----------------------- 3. Run pipeline -------------------------------
WEEK="${WEEK:-$(date -u --date='yesterday' +'%G-W%V')}"

# Pre-sync any existing raw for the target week from S3. Raw data is
# host-local (`data/raw/` is gitignored), but S3 is the cross-host
# source of truth. Without this sync, a freshly-provisioned host sees
# no existing samples and the orchestrator's idempotency check would
# resample everything — burning API budget on data that already exists.
S3_BUCKET="${MERIDIAN_S3_BUCKET:-}"
S3_PREFIX="${MERIDIAN_S3_PREFIX:-meridian/}"
if [ -n "$S3_BUCKET" ]; then
  log "syncing s3://${S3_BUCKET}/${S3_PREFIX}raw/${WEEK}/ → data/raw/${WEEK}/"
  mkdir -p "${REPO_DIR}/data/raw/${WEEK}"
  if ! aws --region "$AWS_REGION" s3 sync \
        "s3://${S3_BUCKET}/${S3_PREFIX}raw/${WEEK}/" \
        "${REPO_DIR}/data/raw/${WEEK}/" \
        --no-progress; then
    log "WARN: s3 sync failed; continuing with local raw only — orchestrator may resample"
  fi
else
  log "MERIDIAN_S3_BUCKET unset; skipping pre-run S3 sync (idempotency is host-local only)"
fi

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
