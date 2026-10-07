#!/usr/bin/env bash
# One-off recovery steps on the EC2 cohabit instance, run through SSM.
# The ordered procedure, with expected output and cost, is the section
# "Recovery: publish 2026-W34 as a partial week and re-classify W36 to
# W38 stance" in scripts/ec2-runbook.md. Read it first.
#
#   sudo -u meridian bash /data/meridian/repo/scripts/recovery.sh <step>
#
# Steps, in order:
#   preflight        refuse if the GPU is busy or a specter process is up
#                    (exit 75: the box is in use, do not stop it); fetch
#                    origin/main, refuse if the local run_log holds rows
#                    origin/main lacks (runlog-check), then reset to
#                    origin/main and uv sync. The checkout may predate this
#                    script, so the runbook runs preflight from
#                    `git show origin/main:scripts/recovery.sh`.
#   runlog-check     fetch origin/main and compare data/run_log.jsonl with
#                    it; no reset. Part of preflight, callable alone.
#   w34-plan         sync raw/2026-W34 from S3, count it, inspect it, and
#                    dry-run recover-week (checks S3 too). No API calls.
#   w34-publish      recover-week --write: stance (Haiku), embeddings,
#                    partial manifest, snapshot, one run_log entry marked
#                    recovery, uploads (never manifests/latest.json).
#   stance-classify  re-classify the 2026-W36 to W38 cells published as
#                    unmeasured, from the committed snapshots, and copy
#                    the results to s3://<bucket>/<prefix>corrections/.
#
# Same environment as scripts/run-weekly.sh: /etc/meridian/config.env
# (MERIDIAN_SECRETS_SSM=1 and the S3 bucket), the meridian user, the
# repo at /data/meridian/repo, uv on PATH. This script does NOT stop the
# instance and is not seen by the reaper (its SSM comment is not the
# orchestrator's), so the operator stops the instance when done.

set -euo pipefail

if [ -f /etc/meridian/config.env ]; then
  # shellcheck disable=SC1091
  set -a
  . /etc/meridian/config.env
  set +a
fi

REPO_DIR="${REPO_DIR:-/data/meridian/repo}"
LOG_DIR="${LOG_DIR:-/data/meridian/logs}"
AWS_REGION="${AWS_DEFAULT_REGION:-us-east-2}"
S3_BUCKET="${MERIDIAN_S3_BUCKET:-meridian-archive-prod}"
S3_PREFIX="${MERIDIAN_S3_PREFIX:-meridian/}"
GPU_MEMORY_THRESHOLD_MB="${GPU_MEMORY_THRESHOLD_MB:-500}"

# The 2026-W34 facts the reconstruction's note and manifest carry.
W34="2026-W34"
W34_ARCHIVED_ON="2026-10-05"
W34_CAUSE="was killed at the one-hour SSM execution timeout"
# The config_hash every scheduled run from 2026-W33 to W40 logged; W34 ran
# under the same config. The reconstructed row records this, not the hash
# of the config the rebuild runs under.
W34_CONFIG_HASH="e78efdfab25cd47d"
STANCE_WEEKS="2026-W36,2026-W37,2026-W38"

STEP="${1:-}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/recovery-${STEP:-none}-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG_FILE") 2>&1
log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

cd "$REPO_DIR"
cli() { uv run python -m meridian.pipeline.cli "$@"; }

# After a normal weekly run the working-tree run_log is HEAD plus that
# week's row, which the instance never commits: run-weekly.sh resets to
# origin/main at the start, then `cli run` appends. The publish workflow
# commits the same row to main later. So the local file differing from
# HEAD is the steady state, not a problem. What a reset must not drop is
# a row origin/main does not have (an unpublished recover-week entry):
# refuse only when the local file is not origin/main and not a prefix of
# it.
runlog_check() {
  timeout 120 git fetch --quiet origin main
  local local_log=data/run_log.jsonl main_log
  main_log=$(mktemp)
  git show origin/main:data/run_log.jsonl > "$main_log"
  if [ ! -f "$local_log" ] || cmp -s "$local_log" "$main_log"; then
    log "run_log: local copy matches origin/main"
  elif head -c "$(wc -c < "$local_log")" "$main_log" | cmp -s - "$local_log"; then
    log "run_log: origin/main is ahead of the local copy (normal after a weekly run); the reset brings it up to date"
  else
    log "REFUSED: data/run_log.jsonl has rows origin/main does not have. A"
    log "reset would drop them; if one is a recover-week entry, publish it"
    log "first (gh workflow run weekly-pipeline.yml -f week=<week>). Diff:"
    diff "$main_log" "$local_log" | head -5 || true
    rm -f "$main_log"
    return 2
  fi
  rm -f "$main_log"
}

case "$STEP" in
  preflight)
    GPU_USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
      | awk '{print $1}' | head -1 || echo 0)
    log "GPU memory used: ${GPU_USED:-0} MB (threshold ${GPU_MEMORY_THRESHOLD_MB})"
    if [ "${GPU_USED:-0}" -gt "$GPU_MEMORY_THRESHOLD_MB" ]; then
      log "REFUSED: the GPU is in use; this is specter's box right now."
      exit 75
    fi
    if pgrep -af 'specter' >/dev/null 2>&1; then
      log "REFUSED: a specter process is running:"; pgrep -af 'specter' | head -5
      exit 75
    fi
    runlog_check || exit 2
    git -c advice.detachedHead=false reset --hard origin/main
    log "repo at $(git rev-parse --short HEAD)"
    timeout 30m uv sync --frozen --group analysis-heavy --group changepoint >/dev/null
    cli recover-week --help | grep -q -- '--archived-on' \
      || { log "REFUSED: this checkout has no recover-week; merge it to main first."; exit 3; }
    log "preflight ok"
    ;;
  runlog-check)
    runlog_check || exit 2
    ;;
  w34-plan)
    mkdir -p "data/raw/$W34"
    aws --region "$AWS_REGION" s3 sync "s3://${S3_BUCKET}/${S3_PREFIX}raw/${W34}/" \
      "data/raw/${W34}/" --no-progress
    log "local raw files for $W34: $(find "data/raw/$W34" -name samples.jsonl | wc -l)"
    for d in "data/raw/$W34"/*/; do
      log "  $(basename "$d"): $(cat "$d"*/samples.jsonl | wc -l) samples"
    done
    cli inspect-week --week "$W34"
    cli recover-week --week "$W34" --archived-on "$W34_ARCHIVED_ON" --cause "$W34_CAUSE" \
      --config-hash "$W34_CONFIG_HASH"
    ;;
  w34-publish)
    cli recover-week --week "$W34" --archived-on "$W34_ARCHIVED_ON" --cause "$W34_CAUSE" \
      --config-hash "$W34_CONFIG_HASH" --write
    aws --region "$AWS_REGION" s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}manifests/${W34}.json"
    aws --region "$AWS_REGION" s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}snapshots/${W34}/responses.jsonl.gz"
    tail -1 data/run_log.jsonl | python3 -c 'import json,sys; d=json.load(sys.stdin); print({k: d.get(k) for k in ("week_id","recovery","runners","config_hash","pairs_complete","pairs_failed","per_runner_samples","actual_cost_usd")})'
    ;;
  stance-classify)
    OUT="/data/meridian/corrections/stance-$(date -u +%Y-%m-%dT%H%M%SZ)"
    uv run python scripts/backfill_stance.py classify --weeks "$STANCE_WEEKS" --out "$OUT"
    aws --region "$AWS_REGION" s3 cp --recursive "$OUT/" \
      "s3://${S3_BUCKET}/${S3_PREFIX}corrections/$(basename "$OUT")/" --no-progress
    log "results: s3://${S3_BUCKET}/${S3_PREFIX}corrections/$(basename "$OUT")/stance-results.jsonl"
    uv run python scripts/backfill_stance.py graft --results "$OUT/stance-results.jsonl" \
      --date "$(date -u +%F)" --dry-run
    ;;
  *)
    echo "usage: $0 preflight|runlog-check|w34-plan|w34-publish|stance-classify" >&2
    exit 64
    ;;
esac
