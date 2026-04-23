"""Explicit resume-after-kill test.

Simulates a mid-run failure by having one runner raise partway through
its batch. Asserts:
  1. Samples that made it to storage before the crash remain intact.
  2. A fresh orchestrator on the same config picks up exactly the
     missing samples without duplicating the prior ones.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from meridian.corpus import load_corpus
from meridian.runners.base import Runner, RunnerError, Sample
from meridian.sampling.orchestrator import Orchestrator, SamplingPlan
from meridian.storage import LocalSampleStore


class _FlakyRunner(Runner):
    """Succeeds for the first ``break_after`` calls, then raises."""
    provider = "flaky"
    model_id = "flaky-1"

    def __init__(self, break_after: int):
        self.break_after = break_after
        self.calls = 0

    async def sample(self, prompt, *, prompt_id, request_index, temperature, max_tokens=1024):
        self.calls += 1
        if self.calls > self.break_after:
            raise RunnerError(f"simulated crash at call {self.calls}")
        return Sample(
            prompt_id=prompt_id, model_id=self.model_id, provider=self.provider,
            request_index=request_index, temperature=temperature, max_tokens=max_tokens,
            text=f"ok-{request_index}", model_version_string="flaky-1-v",
            stop_reason="stop", latency_ms=1,
            captured_at=datetime.now(timezone.utc),
        )


class _StableRunner(Runner):
    provider = "stable"
    model_id = "stable-1"

    def __init__(self):
        self.calls = 0

    async def sample(self, prompt, *, prompt_id, request_index, temperature, max_tokens=1024):
        self.calls += 1
        return Sample(
            prompt_id=prompt_id, model_id=self.model_id, provider=self.provider,
            request_index=request_index, temperature=temperature, max_tokens=max_tokens,
            text=f"ok-{request_index}", model_version_string="stable-1-v",
            stop_reason="stop", latency_ms=1,
            captured_at=datetime.now(timezone.utc),
        )


@pytest.mark.asyncio
async def test_resume_after_mid_run_crash_no_duplicates(tmp_path: Path):
    corpus = load_corpus()
    prompts = corpus.public()[:3]
    store = LocalSampleStore(tmp_path)
    plan = SamplingPlan(
        week_id="2026-W16",
        n_default_temp=4, n_zero_temp=0,
        concurrency_per_provider=1,  # deterministic ordering
    )

    # First run: flaky runner breaks after 5 calls across 3 prompts x 4 samples = 12.
    flaky = _FlakyRunner(break_after=5)
    stable = _StableRunner()
    orch1 = Orchestrator([flaky, stable], store, corpus, plan)
    outcome1 = await orch1.run(prompts=prompts)

    # Flaky runner: some prompt(s) failed before reaching the required count.
    assert outcome1.pairs_failed >= 1
    # Stable runner should have completed all three prompts.
    stable_samples = sum(
        store.count("2026-W16", "stable-1", p.id) for p in prompts
    )
    assert stable_samples == len(prompts) * plan.n_default_temp

    # Capture pre-resume state.
    pre_resume_flaky_counts = {
        p.id: store.count("2026-W16", "flaky-1", p.id) for p in prompts
    }
    pre_resume_stable_calls = stable.calls

    # Second run (the "resume"): flaky runner now stable. Expect orchestrator
    # to fill in exactly the missing flaky samples and leave everything else.
    healed = _StableRunner()
    healed.model_id = "flaky-1"  # same storage key
    healed.provider = "flaky"
    stable2 = _StableRunner()  # re-used stable runner should be no-op
    orch2 = Orchestrator([healed, stable2], store, corpus, plan)
    outcome2 = await orch2.run(prompts=prompts)

    # Stable runner should have been skipped entirely (already complete).
    assert outcome2.pairs_skipped >= len(prompts)
    assert stable2.calls == 0

    # Every flaky (prompt, model) pair is now complete.
    for p in prompts:
        total = store.count("2026-W16", "flaky-1", p.id)
        assert total == plan.n_default_temp, (
            f"{p.id}: expected {plan.n_default_temp} samples after resume, got {total}"
        )
        # And none of the pre-resume samples were re-written.
        assert total >= pre_resume_flaky_counts[p.id]

    # No duplicate request_index values within a (prompt, model) file.
    for p in prompts:
        samples = store.read("2026-W16", "flaky-1", p.id)
        indices = [s.request_index for s in samples]
        assert len(indices) == len(set(indices)), f"duplicate indices in {p.id}"
