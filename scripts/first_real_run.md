# First real pipeline run — runbook

This document is the step-by-step procedure for the project's first run
against live LLM APIs. Follow it in order. Deviations should be recorded
in the run log (`data/run_log.jsonl`) as comments if material.

## Preconditions

- [ ] You are on `main`, clean working tree, latest `uv sync` done.
- [ ] You have active API credentials with a non-trivial credit balance:
  - `ANTHROPIC_API_KEY` — billable (at least $10 of headroom recommended).
  - `OPENAI_API_KEY` — billable (at least $10 of headroom recommended).
- [ ] Ollama is installed and running locally (`ollama serve`), with one
  small model pulled:

      ollama pull llama3.2:3b

- [ ] `drift_audit/config.yaml` has `enabled: true` on the providers you
  intend to run, `false` on the rest. For v0.1 the recommended roster is:

      - anthropic / claude-haiku-4-5-20251001   # cheapest usable Claude
      - openai    / gpt-4.1-mini                # cheapest usable GPT
      - ollama    / llama3.2:3b                 # free local baseline

  Using Haiku and 4.1-mini (not Opus/GPT-5) keeps the first run under $5.

## Pre-flight

```bash
# Verify the corpus loads and the site builds from the current synthetic fixture.
uv run pytest drift_audit/tests/

# Show the estimated cost before committing to a run.
uv run python -m drift_audit.pipeline.cli estimate
```

Expected output for the recommended roster: `total: ~$3.00` on a 30-prompt
public corpus at N=25 samples per (prompt × model).

If the estimate looks wrong, stop and audit `drift_audit/sampling/pricing.py`.

## Run

```bash
# One full week against the enabled providers.
uv run python -m drift_audit.pipeline.cli run --yes
```

Expected wall-clock: 10–30 minutes depending on provider rate limits.
Progress is streamed to stdout per (provider, model, prompt) pair.

If a single pair fails (rate limit, transient error), the orchestrator
continues with the rest and reports failures at the end. Re-running is
safe (idempotent per-pair).

## Post-run

```bash
# Confirm the run log has a fresh entry.
tail -1 data/run_log.jsonl

# Confirm a manifest was written for this week.
ls -la site/fixtures/manifest-*.json

# Rebuild the site from the real manifest.
uv run python site/src/build.py \
    --manifest site/fixtures/manifest-$(date -u +'%Y-W%V').json \
    --out site/dist

# Smoke-preview locally.
python -m http.server 8000 --directory site/dist
```

## Recovery from partial failure

- **One provider's quota exhausted mid-run**: the run completes the other
  providers; the failed pairs remain un-sampled in storage. After topping
  up quota, re-run: only the missing pairs will be sampled.
- **Process killed (SIGINT/SIGTERM)**: storage writes are atomic at the
  sample level, so anything persisted stays persisted. Re-run; the
  orchestrator picks up where it left off.
- **Provider returned garbage for many samples**: do not discard. Flag
  those (prompt × model) pairs in `data/internal/review_queue.jsonl` and
  re-run with `--force` after rotating the prompt text or switching the
  provider model.

## What to record

After the run completes, note in a new GitHub Issue titled
"First real run — {ISO week}":

- Actual cost spent (from the run log).
- Wall-clock elapsed.
- Any errors observed that the retry layer didn't handle cleanly.
- Surprising findings in the rendered dashboard (unexpected refusals,
  format changes, etc.).
- Anything from the silent-update-check CLI:

      uv run python -m drift_audit.pipeline.cli silent-update-check

This issue becomes the anchor for the second run's diff.

## Second run (one week later)

Repeat above. The only change: the site now has real history to plot, so
sparklines and the comparison analyzer produce meaningful output.
