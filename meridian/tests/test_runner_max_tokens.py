"""A per-runner completion cap must reach the API call.

The shared 1024-token cap is wrong for reasoning-default models, which
bill reasoning against it and can exhaust it before emitting output. The
override is only useful if it survives config -> build_runners ->
orchestrator -> runner.batch, so this pins the whole path rather than
the config field in isolation.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from meridian.config import PipelineConfig, RunnerSpec, build_runners
from meridian.runners.base import Runner, Sample
from meridian.sampling.orchestrator import Orchestrator, SamplingPlan
from meridian.storage import LocalSampleStore


def test_config_accepts_per_runner_max_tokens():
    spec = RunnerSpec(provider="openai", model_id="gpt-5.5", max_tokens=8192)
    assert spec.max_tokens == 8192


def test_max_tokens_defaults_to_none_meaning_use_the_shared_cap():
    assert RunnerSpec(provider="openai", model_id="gpt-5.1").max_tokens is None


def test_nonsense_cap_rejected():
    with pytest.raises(ValueError):
        RunnerSpec(provider="openai", model_id="gpt-5.5", max_tokens=0)


def test_build_runners_propagates_the_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    config = PipelineConfig(runners=[
        RunnerSpec(provider="openai", model_id="gpt-5.5", max_tokens=8192),
        RunnerSpec(provider="anthropic", model_id="claude-opus-4-8"),
    ])
    by_id = {r.model_id: r for r in build_runners(config)}
    assert by_id["gpt-5.5"].max_tokens_override == 8192
    assert by_id["claude-opus-4-8"].max_tokens_override is None


def test_shipped_config_raises_the_cap_for_gpt_5_5():
    """Regression guard on the actual deployed value: this is the fix
    for the 2026-W27/W29 truncation loss, and silently reverting to the
    shared cap would reintroduce it."""
    from meridian.config import load_config
    config = load_config()
    gpt = next(
        (s for s in config.runners if s.model_id.startswith("gpt-5.5")), None
    )
    assert gpt is not None, "gpt-5.5 missing from config.yaml"
    assert gpt.max_tokens is not None and gpt.max_tokens > config.sampling.max_tokens


class _RecordingRunner(Runner):
    provider = "fake"

    def __init__(self, model_id: str, max_tokens: int | None = None) -> None:
        self.model_id = model_id
        self.max_tokens_override = max_tokens
        self.seen: list[int] = []

    async def sample(self, prompt, *, prompt_id, request_index,
                     temperature, max_tokens=1024) -> Sample:
        self.seen.append(max_tokens)
        return Sample(
            prompt_id=prompt_id, model_id=self.model_id, provider=self.provider,
            request_index=request_index, temperature=temperature,
            max_tokens=max_tokens, text="an answer",
            model_version_string="v1", finish_reason="stop",
            latency_ms=1, captured_at=datetime.now(timezone.utc),
        )


@pytest.mark.asyncio
async def test_orchestrator_uses_override_and_falls_back(tmp_path):
    from meridian.corpus import load_corpus
    corpus = load_corpus()
    prompts = corpus.public()[:1]

    overridden = _RecordingRunner("reasoner", max_tokens=8192)
    plain = _RecordingRunner("ordinary")
    plan = SamplingPlan(week_id="2026-W30", n_default_temp=2, n_zero_temp=0,
                        max_tokens=1024)
    store = LocalSampleStore(tmp_path)
    await Orchestrator([overridden, plain], store, corpus, plan).run(prompts=prompts)

    assert set(overridden.seen) == {8192}
    assert set(plain.seen) == {1024}
