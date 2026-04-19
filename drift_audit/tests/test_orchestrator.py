"""Orchestrator behavior: idempotency, iteration, error capture."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from drift_audit.corpus import load_corpus
from drift_audit.runners.base import Runner, RunnerError, Sample
from drift_audit.sampling.orchestrator import Orchestrator, SamplingPlan
from drift_audit.storage import LocalSampleStore


class _FakeRunner(Runner):
    provider = "fake"

    def __init__(self, model_id: str, text: str = "ok"):
        self.model_id = model_id
        self._text = text
        self.call_count = 0

    async def sample(
        self,
        prompt: str,
        *,
        prompt_id: str,
        request_index: int,
        temperature: float,
        max_tokens: int = 1024,
    ) -> Sample:
        self.call_count += 1
        return Sample(
            prompt_id=prompt_id,
            model_id=self.model_id,
            provider=self.provider,
            request_index=request_index,
            temperature=temperature,
            max_tokens=max_tokens,
            text=f"{self._text}:{request_index}",
            model_version_string=self.model_id + "-fake",
            stop_reason="stop",
            latency_ms=1,
            captured_at=datetime.now(timezone.utc),
        )


class _BrokenRunner(Runner):
    provider = "broken"
    model_id = "broken-1"

    async def sample(self, prompt: str, *, prompt_id, request_index, temperature, max_tokens=1024):
        raise RunnerError("simulated upstream failure")


@pytest.mark.asyncio
async def test_orchestrator_writes_expected_sample_count(tmp_path: Path):
    corpus = load_corpus()
    # Trim corpus to 2 prompts to keep the test fast.
    one_axis = corpus.by_axis("scientific-consensus")[:2]
    store = LocalSampleStore(tmp_path)
    runner = _FakeRunner("llama3.2:3b")
    plan = SamplingPlan(
        week_id="2026-W16",
        n_default_temp=3,
        n_zero_temp=1,
        concurrency_per_provider=2,
    )
    orch = Orchestrator([runner], store, corpus, plan)

    outcome = await orch.run(prompts=one_axis)

    # 2 prompts * (3 + 1) samples = 8 samples total.
    assert outcome.total_samples_written == 8
    assert outcome.pairs_complete == 2
    assert outcome.pairs_failed == 0
    for prompt in one_axis:
        assert store.count("2026-W16", "llama3.2:3b", prompt.id) == 4


@pytest.mark.asyncio
async def test_orchestrator_is_idempotent_by_default(tmp_path: Path):
    corpus = load_corpus()
    one_axis = corpus.by_axis("scientific-consensus")[:1]
    store = LocalSampleStore(tmp_path)
    runner = _FakeRunner("llama3.2:3b")
    plan = SamplingPlan(week_id="2026-W16", n_default_temp=3, n_zero_temp=1)

    orch = Orchestrator([runner], store, corpus, plan)
    await orch.run(prompts=one_axis)
    calls_first = runner.call_count

    outcome = await orch.run(prompts=one_axis)
    assert runner.call_count == calls_first, "second run should make zero new calls"
    assert outcome.pairs_skipped == 1
    assert outcome.total_samples_written == 0


@pytest.mark.asyncio
async def test_orchestrator_force_reruns(tmp_path: Path):
    corpus = load_corpus()
    one_axis = corpus.by_axis("scientific-consensus")[:1]
    store = LocalSampleStore(tmp_path)
    runner = _FakeRunner("llama3.2:3b")
    plan = SamplingPlan(week_id="2026-W16", n_default_temp=3, n_zero_temp=1)

    orch = Orchestrator([runner], store, corpus, plan)
    await orch.run(prompts=one_axis)
    calls_first = runner.call_count

    await orch.run(prompts=one_axis, force=True)
    assert runner.call_count > calls_first


@pytest.mark.asyncio
async def test_orchestrator_collects_errors_without_halting(tmp_path: Path):
    corpus = load_corpus()
    one_axis = corpus.by_axis("scientific-consensus")[:2]
    store = LocalSampleStore(tmp_path)
    broken = _BrokenRunner()
    ok = _FakeRunner("llama3.2:3b")
    plan = SamplingPlan(week_id="2026-W16", n_default_temp=2, n_zero_temp=0)
    orch = Orchestrator([broken, ok], store, corpus, plan)

    outcome = await orch.run(prompts=one_axis)

    assert outcome.pairs_failed == 2   # broken failed on both prompts
    assert outcome.pairs_complete == 2 # ok succeeded on both
    assert outcome.total_samples_written == 4  # only from ok runner
    assert {e.provider for e in outcome.errors} == {"broken"}
