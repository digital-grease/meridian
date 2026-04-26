"""Integration test: stance classifier → manifest → metric records.

The class under test is the wiring path the production pipeline uses:
``stance_collect.collect_stance_results`` running a stub classifier
across stored samples, the result feeding into
``manifest_writer.build_manifest`` as ``stance_by_key``, and the final
metric records carrying real stance values instead of "na".

A pre-fix run of this test against the v0 pipeline (CLI not passing
stance_by_key) would have left every metric record's stance="na" —
the gap the originating plan was opened to close.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from meridian.analysis.stance import StanceResult
from meridian.corpus import load_corpus
from meridian.pipeline.manifest_writer import RunnerDisplayInfo, build_manifest
from meridian.pipeline.stance_collect import collect_stance_results
from meridian.runners.base import Sample
from meridian.storage import LocalSampleStore


class _StubStanceClassifier:
    """Deterministic stance classifier keyed on prompt_id.

    Maps the corpus's known political and historical prompt_ids to
    fixed stances so the assertion can pin specific cells.
    """

    _MAP = {
        "pol-abortion-legal": ("pro", 0.9),
        "pol-gun-control": ("anti", 0.9),
        "pol-israel-palestine": ("neutral", 0.9),
        "hist-tiananmen-1989": ("anti", 0.9),
        "hist-armenian-genocide": ("pro", 0.9),
    }

    async def classify(self, *, prompt_id, axis, prompt_text, response_text):
        if prompt_id in self._MAP:
            stance, confidence = self._MAP[prompt_id]
            return StanceResult(stance=stance, confidence=confidence, reason=None)
        return StanceResult(stance="na", confidence=1.0, reason="stub-no-mapping")


def _seed_samples(store: LocalSampleStore, week_id: str, model_id: str, prompt_ids: list[str]):
    """Write 3 substantive samples per pair so the representative-response
    selector has something to pick from."""
    for pid in prompt_ids:
        for i in range(3):
            store.append(week_id, model_id, pid, Sample(
                prompt_id=pid, model_id=model_id, provider="stub",
                request_index=i, temperature=1.0, max_tokens=1024,
                text=f"Substantive response number {i} on {pid}. "
                     "Reasonable people may disagree, but on balance, yes.",
                model_version_string=f"{model_id}-stub",
                stop_reason="stop", latency_ms=1,
                captured_at=datetime.now(timezone.utc),
            ))


@pytest.mark.asyncio
async def test_stance_results_propagate_to_metric_records(tmp_path: Path):
    week_id = "2026-W16"
    model_id = "stub-model"
    corpus = load_corpus()

    political_pids = [p.id for p in corpus.by_axis("political")][:3]
    historical_pids = [p.id for p in corpus.by_axis("historical-contested")][:2]
    neutral_pids = [p.id for p in corpus.by_axis("neutral-control")][:1]

    store = LocalSampleStore(tmp_path)
    _seed_samples(store, week_id, model_id, political_pids + historical_pids + neutral_pids)

    classifier = _StubStanceClassifier()
    stance_by_key = await collect_stance_results(
        classifier=classifier, store=store, corpus=corpus, week_id=week_id,
    )

    # Sanity: every prompt + model pair is represented in the result map.
    expected_pairs = {(pid, model_id) for pid in
                      political_pids + historical_pids + neutral_pids}
    assert set(stance_by_key) == expected_pairs

    # Stance-bearing axes should have classifier-emitted stances.
    for pid in political_pids + historical_pids:
        assert stance_by_key[(pid, model_id)].stance in {"pro", "anti", "neutral"}

    # Neutral-control axis is always na (axis-excluded), even with our
    # stub. The collector short-circuits before invoking the classifier.
    for pid in neutral_pids:
        result = stance_by_key[(pid, model_id)]
        assert result.stance == "na"
        assert result.reason == "axis-excluded"

    # Manifest writer carries the stance through to the per-metric record.
    manifest = build_manifest(
        store=store, corpus=corpus, week_id=week_id,
        display_info={
            model_id: RunnerDisplayInfo(model_id, "Stub", "stub"),
        },
        bootstrap_seed=1,
        stance_by_key=stance_by_key,
    )

    metrics_by_key = {(m["prompt_id"], m["model_id"]): m for m in manifest["metrics"]}
    # Spot-check known mappings from the stub.
    assert metrics_by_key[("pol-abortion-legal", model_id)]["stance"] == "pro"
    assert metrics_by_key[("pol-gun-control", model_id)]["stance"] == "anti"
    assert metrics_by_key[("hist-tiananmen-1989", model_id)]["stance"] == "anti"
    # Neutral-control prompts stay "na".
    for pid in neutral_pids:
        assert metrics_by_key[(pid, model_id)]["stance"] == "na"


@pytest.mark.asyncio
async def test_collect_handles_all_refusals_as_na(tmp_path: Path):
    """If every sample for a (prompt × model) pair is a refusal, the
    collector should not invoke the classifier and should mark the
    pair na with a 'no-substantive-response' reason."""
    week_id = "2026-W16"
    model_id = "stub-model"
    corpus = load_corpus()
    political_pids = [p.id for p in corpus.by_axis("political")][:1]

    store = LocalSampleStore(tmp_path)
    for pid in political_pids:
        for i in range(3):
            store.append(week_id, model_id, pid, Sample(
                prompt_id=pid, model_id=model_id, provider="stub",
                request_index=i, temperature=1.0, max_tokens=1024,
                text="I can't help with that request.",
                model_version_string=f"{model_id}-stub",
                stop_reason="stop", latency_ms=1,
                captured_at=datetime.now(timezone.utc),
            ))

    invocation_count = 0

    class _Counter:
        async def classify(self, **_):
            nonlocal invocation_count
            invocation_count += 1
            return StanceResult(stance="pro", confidence=1.0)

    out = await collect_stance_results(
        classifier=_Counter(), store=store, corpus=corpus, week_id=week_id,
    )

    assert invocation_count == 0, "classifier should be skipped when all samples refuse"
    pid = political_pids[0]
    assert out[(pid, model_id)].stance == "na"
    assert out[(pid, model_id)].reason == "no-substantive-response"
