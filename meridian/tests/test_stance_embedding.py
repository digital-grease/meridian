"""Stance classifier + embedding drift tests. Fake deps throughout."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from meridian.analysis.embedding import centroid_shift
from meridian.analysis.stance import (
    LLMStanceClassifier,
    StanceResult,
    parse_stance,
)
from meridian.runners.base import Runner, Sample


# --- Stance ---


class _ScriptedRunner(Runner):
    """Runner that returns the next text from a queue each call."""
    provider = "fake"
    model_id = "fake-stance"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def sample(self, prompt, *, prompt_id, request_index, temperature, max_tokens=1024):
        self.calls += 1
        text = self._responses.pop(0) if self._responses else "na"
        return Sample(
            prompt_id=prompt_id, model_id=self.model_id, provider=self.provider,
            request_index=request_index, temperature=temperature, max_tokens=max_tokens,
            text=text, model_version_string=self.model_id + "-v",
            stop_reason="stop", latency_ms=1,
            captured_at=datetime.now(timezone.utc),
        )


@pytest.mark.asyncio
async def test_stance_na_on_non_stance_axes_skips_llm():
    runner = _ScriptedRunner(["would be pro"])
    classifier = LLMStanceClassifier(runner)
    r = await classifier.classify(
        prompt_id="sci-evolution", axis="scientific-consensus",
        prompt_text="Explain evolution.",
        response_text="Evolution works via natural selection.",
    )
    assert r.stance == "na"
    assert r.reason == "axis-excluded"
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_stance_calls_llm_on_stance_axis():
    runner = _ScriptedRunner(["pro"])
    classifier = LLMStanceClassifier(runner)
    r = await classifier.classify(
        prompt_id="pol-abortion-legal", axis="political",
        prompt_text="Should abortion be legal?",
        response_text="Yes, abortion should be legal.",
    )
    assert r.stance == "pro"
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_stance_cache_suppresses_duplicate_calls(tmp_path: Path):
    runner = _ScriptedRunner(["neutral", "pro"])  # only first should be consumed
    cache = tmp_path / "stance-cache.jsonl"
    classifier = LLMStanceClassifier(runner, cache_path=cache)
    r1 = await classifier.classify(
        prompt_id="pol-x", axis="political",
        prompt_text="q", response_text="same response text",
    )
    r2 = await classifier.classify(
        prompt_id="pol-x", axis="political",
        prompt_text="q", response_text="same response text",
    )
    assert r1.stance == r2.stance == "neutral"
    assert runner.calls == 1  # cache hit on the second call
    assert cache.exists()


@pytest.mark.asyncio
async def test_stance_cache_persists_across_instances(tmp_path: Path):
    cache = tmp_path / "stance-cache.jsonl"
    runner1 = _ScriptedRunner(["anti"])
    c1 = LLMStanceClassifier(runner1, cache_path=cache)
    await c1.classify(
        prompt_id="pol-x", axis="political",
        prompt_text="q", response_text="body",
    )
    # New instance should read the cache file and skip the runner call.
    runner2 = _ScriptedRunner([])  # no responses available
    c2 = LLMStanceClassifier(runner2, cache_path=cache)
    r = await c2.classify(
        prompt_id="pol-x", axis="political",
        prompt_text="q", response_text="body",
    )
    assert r.stance == "anti"
    assert runner2.calls == 0


def test_parse_stance_variants():
    assert parse_stance("pro").stance == "pro"
    assert parse_stance("The stance is: anti").stance == "anti"
    assert parse_stance("NEUTRAL").stance == "neutral"
    assert parse_stance("na").stance == "na"
    assert parse_stance("not sure").stance == "na"  # unparseable -> na


# --- Embedding centroid ---


class _FakeEmbedder:
    """Deterministic fake that hashes each text to an 8-dim unit-ish vector.
    Identical texts get identical vectors; different texts get different ones
    with no accidental near-colinearity."""

    def encode(self, texts):
        import hashlib
        import numpy as np
        rows = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            rows.append([(h[i] - 128) / 128.0 for i in range(8)])
        return np.array(rows, dtype=float)


def test_centroid_shift_zero_when_texts_match():
    model = _FakeEmbedder()
    texts = ["hello world", "goodbye moon", "hello there"]
    # Same texts -> zero shift.
    assert centroid_shift(texts, texts, model) == 0.0


def test_centroid_shift_positive_when_texts_differ():
    model = _FakeEmbedder()
    a = ["ABC", "ABD"]           # both start with 'A'
    b = ["ZZZZ", "ZZZZA"]        # both start with 'Z', longer
    shift = centroid_shift(a, b, model)
    assert shift is not None
    assert shift > 0.0


def test_centroid_shift_none_on_empty():
    model = _FakeEmbedder()
    assert centroid_shift([], ["anything"], model) is None
    assert centroid_shift(["anything"], [], model) is None
