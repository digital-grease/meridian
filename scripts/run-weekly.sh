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
#   4. Publish a structured outcome to SNS regardless of success/failure,
#      judged by scripts/check_run_health.py rather than by exit code
#      alone (a run can exit 0 with unusable samples in it).
#   5. If WE_OWN_LIFECYCLE=1, stop the EC2 instance after we're done, on
#      EVERY exit path, and alert if the stop fails. Enforced by an EXIT
#      trap plus signal traps, not by a call at the bottom of the file,
#      and backed by timeouts on every long-running step so that a hang
#      cannot outlive the run either. See the trap block below for why.
#
# Exit codes:
#   0  clean run (or deferred without contention being a real problem)
#   1  pipeline run failed
#   2  pre-flight contention detected — deferred
#   3  config or environment issue
#
# Past the pre-flight the pipeline's own exit code is passed through, so a
# 2 from there means a fatal config/auth/storage error or a --max-cost
# ceiling stop, not contention. Read the SNS subject, not the code; nothing
# consumes this exit status (SSM dispatch is fire-and-forget).
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

# ----------------------- 0. Self-update + re-exec ----------------------
# This wrapper lives inside the repo it deploys. Updating the repo with
# `git reset --hard` does NOT change the code the running shell executes:
# bash holds this file's original inode open and keeps reading it, so
# every edit to the steps below would otherwise not take effect until the
# *next* weekly run — and a stale plain `uv sync` would prune the heavy
# analysis deps (sentence-transformers / ruptures) on each lagged run.
# Pull first, then re-exec the freshly-pulled copy exactly once; the
# sentinel env var guards against a re-exec loop.
REPO_DIR="${REPO_DIR:-/data/meridian/repo}"
if [ -z "${MERIDIAN_WRAPPER_REEXECED:-}" ] && [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" fetch --quiet origin main || true
  git -C "$REPO_DIR" -c advice.detachedHead=false reset --hard origin/main || true
  export MERIDIAN_WRAPPER_REEXECED=1
  exec bash "$REPO_DIR/scripts/run-weekly.sh" "$@"
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
  #
  # KEEP EVERY SUBJECT PRINTABLE ASCII. SNS documents Subject that way and
  # rejects anything else, and the publish below swallows a failure into a
  # log line nobody reads, so a subject containing an em-dash is an alert
  # that silently never arrives. The alerts most likely to carry decorative
  # punctuation are the ATTENTION ones about a g5.2xlarge that is still
  # running at roughly $1/hour, which is the exact alert least affordable
  # to lose. Commas and colons only.
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

# Set once the stop has been attempted, so the EXIT trap and any
# belt-and-braces caller cannot double-publish the failure alert.
SELF_STOP_ATTEMPTED=0

self_stop_if_needed() {
  # A g5.2xlarge left running costs roughly $1/hour against a project
  # API budget of about $45/month, so a stop that fails silently is one
  # of the more expensive failure modes in the system. Both branches
  # below used to go through `log`, which is visible only to whoever
  # reads the SSM output or the on-instance log file, i.e. nobody, until
  # the bill arrives. They alert now.
  #
  # This runs from an EXIT trap, so it must never abort the shell itself:
  # `set +e` locally, and keep every aws call inside an `if`. A failure in
  # here that killed the trap would be the exact bug this function exists
  # to prevent.
  set +e
  if [ "$SELF_STOP_ATTEMPTED" = "1" ]; then
    return 0
  fi
  SELF_STOP_ATTEMPTED=1
  if [ "$WE_OWN_LIFECYCLE" = "1" ]; then
    if [ -z "$INSTANCE_ID" ]; then
      log "WE_OWN_LIFECYCLE=1 but couldn't resolve INSTANCE_ID via IMDSv2; not stopping."
      publish "ATTENTION: instance still running, could not self-stop" \
        "WE_OWN_LIFECYCLE=1 but the instance id could not be resolved via IMDSv2, so no ec2 stop-instances was attempted. The instance is still running and billing (~\$1/hr for a g5.2xlarge). Stop it by hand: aws ec2 stop-instances --instance-ids <id>."
      return
    fi
    log "stopping instance $INSTANCE_ID (we own lifecycle)"
    if ! aws --region "$AWS_REGION" ec2 stop-instances --instance-ids "$INSTANCE_ID" >/dev/null; then
      log "stop-instances failed; instance will continue running until manual intervention"
      publish "ATTENTION: instance still running, stop-instances failed" \
        "ec2 stop-instances failed for $INSTANCE_ID in $AWS_REGION. The instance keeps running and billing (~\$1/hr for a g5.2xlarge) until someone intervenes. Stop it by hand: aws --region $AWS_REGION ec2 stop-instances --instance-ids $INSTANCE_ID"
    fi
  else
    log "leaving instance running; we did not start it (WE_OWN_LIFECYCLE=$WE_OWN_LIFECYCLE)"
  fi
}

# THIS TRAP IS THE GUARANTEE for the exit paths a shell can observe. Do
# not remove it, and do not move the stop back to a call at the bottom of
# the file, where it lived until 2026-08-26: `set -e` is switched on
# partway through step 3, so anything exiting between there and the end
# skipped the stop entirely.
#
# Be clear about what this trap does NOT cover, because the incident that
# prompted writing it is in that category. 2026-W34 was SIGKILLed by SSM
# at the AWS-RunShellScript `executionTimeout` document default of 3600s,
# and SIGKILL cannot be trapped, so no amount of care in this file would
# have stopped that box; it billed roughly 18 idle hours. That cause is
# fixed outside this script, in the orchestrator's now-explicit
# executionTimeout and in the meridian-reaper backstop (commit 8291b6c).
#
# What this trap buys is the difference between stopping immediately and
# waiting up to an hour for the reaper, on every failure the shell CAN
# see: falling off the end, an explicit `exit`, or `set -e` aborting.
# TERM/INT/HUP are converted into ordinary exits below so they reach it
# too, and a hang is covered by the timeout on the pipeline step. The
# reaper exists because the wrapper cannot be the only thing that stops
# the instance. This trap exists so the reaper is not the usual one.

# Converting a fatal signal into an ordinary `exit` is what lets the EXIT
# trap run at all. Killing the pipeline first matters because bash only
# reaches this handler from an interruptible `wait` (see step 3), and
# leaving the child alive would keep the GPU pinned on a box we are about
# to stop, or under WE_OWN_LIFECYCLE=0 on somebody else's machine.
on_signal() {
  local code="$1"
  if [ -n "${PIPELINE_PID:-}" ] && kill -0 "$PIPELINE_PID" 2>/dev/null; then
    log "signal received, terminating pipeline pid $PIPELINE_PID"
    kill -TERM "$PIPELINE_PID" 2>/dev/null || true
  fi
  exit "$code"
}

trap self_stop_if_needed EXIT
trap 'on_signal 143' TERM
trap 'on_signal 130' INT
trap 'on_signal 129' HUP

# ----------------------- 1. Pre-flight ---------------------------------
log "pre-flight: checking GPU memory"
GPU_USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
  | awk '{print $1}' | head -1 || echo 0)
log "GPU memory used: ${GPU_USED} MB (threshold ${GPU_MEMORY_THRESHOLD_MB})"
# Deferring is not a reason to leave a g5.2xlarge running. The rule is
# who started it, not why we are quitting: WE_OWN_LIFECYCLE=1 means the
# orchestrator Lambda cold-started this box minutes ago specifically for
# us, so nothing else can be depending on it and we pay ~$1/hr for every
# hour we walk away. WE_OWN_LIFECYCLE=0 means the instance was already up
# when the Lambda tried to start it, i.e. specter is using it, and
# stopping it would kill somebody else's work. Both defer paths below
# previously exited without stopping in either case.
#
# That reasoning rests on one premise, and the premise is the part worth
# recording: nothing autostarts a GPU workload on specter at boot, so a
# box we cold-started and then deferred on is genuinely idle and not a
# machine somebody else's job is about to land on. Owner-confirmed
# 2026-08-15. If that ever stops being true, this is the line to revisit
# first, because the defer path would then be racing a workload that
# started between the Lambda's StartInstances and our pre-flight check.
defer_and_exit() {
  local subject="$1" body="$2"
  publish "$subject" "$body"
  # No self_stop_if_needed here: the EXIT trap runs it on the way out of
  # this `exit 2`, and the guard inside it makes the ordering irrelevant.
  exit 2
}

if [ "$GPU_USED" -gt "$GPU_MEMORY_THRESHOLD_MB" ]; then
  defer_and_exit "deferred, GPU busy at start" \
    "GPU memory in use: ${GPU_USED} MB. Specter or another workload is on the instance. Skipping this week per the no-backfill policy. WE_OWN_LIFECYCLE=$WE_OWN_LIFECYCLE (1 means this instance is being stopped again now)."
fi

log "pre-flight: scanning for specter processes"
if pgrep -af 'specter' >/dev/null 2>&1; then
  PROC_DETAIL=$(pgrep -af 'specter' | head -5)
  defer_and_exit "deferred, specter process detected" \
    "pgrep matched:\n${PROC_DETAIL}\nSkipping this week per the no-backfill policy. WE_OWN_LIFECYCLE=$WE_OWN_LIFECYCLE (1 means this instance is being stopped again now)."
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
  publish "FAILED: ollama not reachable" "ollama did not respond on http://localhost:11434/api/tags within 180s. Check 'systemctl status ollama' on the instance. The instance is stopped again if we started it; logs survive on the EBS volume at $LOG_FILE."
  # Same reasoning as defer_and_exit: an environment fault is no reason
  # to keep paying for a GPU box we cold-started. The log file lives on
  # the persistent EBS volume, so stopping does not cost the operator
  # anything they need for the post-mortem. The EXIT trap does the stop.
  exit 3
fi

# ----------------------- 2. Repo + deps --------------------------------
if [ ! -d "$REPO_DIR/.git" ]; then
  publish "FAILED: repo not present" "Expected meridian repo at $REPO_DIR but no .git directory found. Run the on-instance bootstrap and clone the repo before triggering."
  exit 3
fi

# The repo was already fetched and hard-reset to origin/main in step 0
# (before the re-exec), so the code running now is current. cd in for the
# uv / pipeline commands below.
cd "$REPO_DIR"
log "repo at $(git rev-parse --short HEAD) (fetched + reset in step 0)"

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
# Each resolve is bounded for the same reason the pipeline is: these are
# network calls on a box that bills by the hour, and a registry that
# accepts the connection but never answers would otherwise wedge the run
# before it reaches the pipeline's own timeout.
log "uv sync (+ analysis-heavy, changepoint)"
UV_SYNC_TIMEOUT="${UV_SYNC_TIMEOUT:-30m}"
timeout "$UV_SYNC_TIMEOUT" uv sync --frozen --group analysis-heavy --group changepoint >/dev/null \
  || timeout "$UV_SYNC_TIMEOUT" uv sync --exclude-newer "7 days" --group analysis-heavy --group changepoint >/dev/null \
  || timeout "$UV_SYNC_TIMEOUT" uv sync --group analysis-heavy --group changepoint >/dev/null

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
  if ! timeout "${S3_SYNC_TIMEOUT:-30m}" aws --region "$AWS_REGION" s3 sync \
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
# --max-cost is a hard ceiling in USD for this invocation, gating the
# pre-flight ESTIMATE rather than actual spend.
#
# The headroom is no longer generous. As of 2026-08-16 an even week
# estimates $27.45 (Opus 4.8 plus Opus 5) and an odd week $22.91
# (gpt-5.5), so 40 leaves 31% on the even week rather than the
# comfortable margin this comment used to claim. Raising a completion
# cap or adding a fourth paid runner will cross it.
#
# That is the intended behaviour, not a misconfiguration: an unattended
# job that has become several times more expensive should stop and ask.
# If a run aborts here, find out WHICH change moved the number before
# raising the ceiling. See meridian/BUDGET.md for the current per-week
# figures.
MAX_COST_USD="${MAX_COST_USD:-40}"

# A hang is the one runaway shape the EXIT trap cannot catch: a wedged
# process never exits, so the trap never fires, and the instance bills at
# ~$1/hr for as long as it stays wedged.
#
# THIS MUST STAY BELOW ssm_execution_timeout_seconds (21600s / 6h as of
# 8291b6c). Both bound the same run, but SSM's ceiling is a SIGKILL this
# script cannot trap, which is precisely how 2026-W34 billed 18 idle
# hours. Firing first means the run ends on a trappable TERM, the EXIT
# trap runs, and the box stops itself rather than waiting for the reaper.
#
# Sized against the real run, which is no longer the 26 minutes an earlier
# version of this comment assumed: ca9b0cc put opus-5 alongside opus-4-8
# and took the run to about 2h10m. 5h is a bit over 2x that and still an
# hour clear of SSM's ceiling. --kill-after escalates to KILL if the
# pipeline ignores TERM. timeout exits 124 on expiry, which lands in the
# FAILED branch below and sends the usual SNS alert, so a timed-out week
# is loud rather than silent.
PIPELINE_TIMEOUT="${PIPELINE_TIMEOUT:-5h}"

# Run the pipeline in the BACKGROUND and `wait` on it, rather than as an
# ordinary foreground command. This is not a style choice. bash does not
# run a trap handler while it is waiting on a foreground child: the signal
# is recorded and the handler deferred until that child exits. With the
# pipeline in the foreground, a TERM arriving mid-run would sit unhandled
# until the pipeline finished on its own, which for a wedged run is never,
# and the instance would keep billing exactly as it did on 2026-08-24.
# `wait` is interruptible, so the signal traps fire immediately.
timeout --signal=TERM --kill-after=60s "$PIPELINE_TIMEOUT" \
  uv run python -m meridian.pipeline.cli run --week "$WEEK" --yes --max-cost "$MAX_COST_USD" &
PIPELINE_PID=$!
wait "$PIPELINE_PID"
RUN_RC=$?
set -e
if [ "$RUN_RC" -eq 124 ]; then
  log "pipeline exceeded PIPELINE_TIMEOUT=$PIPELINE_TIMEOUT and was killed"
fi
PIPELINE_END_EPOCH=$(date -u +%s)
ELAPSED=$((PIPELINE_END_EPOCH - PIPELINE_START_EPOCH))

# ----------------------- 4. Report -------------------------------------
RUN_LOG_TAIL=$(tail -1 "$REPO_DIR/data/run_log.jsonl" 2>/dev/null || echo "(no run_log entry)")

# The exit code is not the whole story. A run can exit 0 having stored
# samples that carry no usable content: on 2026-08-10, 20 of the week's
# samples came back empty and this script emailed "weekly run succeeded"
# anyway, because it branched on $? alone. check_run_health.py already
# knows how to read that out of the run-log entry (it is what the publish
# workflow runs in CI), so run the same judge here instead of a second,
# divergent copy of the rules.
#
# ITS EXIT CODES ARE A THREE-WAY CONTRACT, not pass/fail:
#   0  clean
#   3  warn  (the run is usable but something needs a human look)
#   1  fail  (the week is not comparable, or there is no run-log entry)
# Until 2026-08 it returned 0 for both clean and warn, so the branch
# below that exists to catch a warning could not fire at all: the only
# way to reach it was a hard failure. Do not collapse this back to
# `-ne 0`, and if the judge ever gains a fourth code, add it here rather
# than letting it fall through to the failure branch by accident.
HEALTH_RC=0
HEALTH_DETAIL="(health check not run)"
if [ -f "$REPO_DIR/scripts/check_run_health.py" ]; then
  set +e
  HEALTH_DETAIL=$(uv run python "$REPO_DIR/scripts/check_run_health.py" "$WEEK" \
    --run-log "$REPO_DIR/data/run_log.jsonl" 2>&1)
  HEALTH_RC=$?
  set -e
  log "run health check rc=$HEALTH_RC: $HEALTH_DETAIL"
fi

if [ "$RUN_RC" -eq 0 ] && [ "$HEALTH_RC" -eq 3 ]; then
  publish "weekly run completed with warnings ($WEEK)" \
    "The pipeline exited 0 and the run is usable, but the health check raised a warning. Affected cells may be under-sampled, so read the detail before treating this week as comparable.\n\nHealth check:\n${HEALTH_DETAIL}\n\nWall-clock: ${ELAPSED}s\nWE_OWN_LIFECYCLE=$WE_OWN_LIFECYCLE\n\nRun-log entry:\n${RUN_LOG_TAIL}"
  log "pipeline succeeded in ${ELAPSED}s but health check warned (rc=3)"
elif [ "$RUN_RC" -eq 0 ] && [ "$HEALTH_RC" -ne 0 ]; then
  publish "weekly run NOT HEALTHY ($WEEK, health rc=$HEALTH_RC)" \
    "The pipeline exited 0 but the health check judged the week unusable. This is the 2026-08-10 shape: a run that reports success while its samples carry nothing measurable. Do not treat this week as comparable until someone has read the detail.\n\nHealth check:\n${HEALTH_DETAIL}\n\nWall-clock: ${ELAPSED}s\nWE_OWN_LIFECYCLE=$WE_OWN_LIFECYCLE\n\nRun-log entry:\n${RUN_LOG_TAIL}"
  log "pipeline succeeded in ${ELAPSED}s but health check FAILED the week (rc=$HEALTH_RC)"
elif [ "$RUN_RC" -eq 0 ]; then
  publish "weekly run succeeded ($WEEK)" \
    "Wall-clock: ${ELAPSED}s\nWE_OWN_LIFECYCLE=$WE_OWN_LIFECYCLE\n\nHealth check:\n${HEALTH_DETAIL}\n\nRun-log entry:\n${RUN_LOG_TAIL}"
  log "pipeline succeeded in ${ELAPSED}s"
else
  publish "weekly run FAILED ($WEEK, rc=$RUN_RC)" \
    "Wall-clock: ${ELAPSED}s\nWE_OWN_LIFECYCLE=$WE_OWN_LIFECYCLE\n\nLog tail:\n$(tail -80 "$LOG_FILE")\n\nRun-log entry (if any):\n${RUN_LOG_TAIL}"
  log "pipeline FAILED with rc=$RUN_RC in ${ELAPSED}s"
fi

# ----------------------- 5. Self-stop ----------------------------------
# Deliberately nothing here. The EXIT trap installed next to
# self_stop_if_needed stops the instance on this exit and on every other
# one, which is the whole point of moving it out of this position.
exit "$RUN_RC"
