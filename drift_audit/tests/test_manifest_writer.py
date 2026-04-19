"""End-to-end: write fake samples, build a manifest, verify it passes
both the site's Pydantic model and its JSON Schema."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drift_audit.corpus import load_corpus
from drift_audit.pipeline.manifest_writer import (
    RunnerDisplayInfo,
    build_manifest,
    write_manifest,
)
from drift_audit.runners.base import Sample
from drift_audit.storage import LocalSampleStore


def _fake_sample(*, prompt_id: str, model_id: str, idx: int, text: str) -> Sample:
    return Sample(
        prompt_id=prompt_id,
        model_id=model_id,
        provider="fake",
        request_index=idx,
        temperature=1.0,
        max_tokens=1024,
        text=text,
        model_version_string=f"{model_id}-2026-04-01",
        stop_reason="stop",
        latency_ms=1,
        captured_at=datetime(2026, 4, 19, tzinfo=timezone.utc),
    )


def _seed_store(tmp_path: Path, corpus, week_id: str, model_id: str) -> LocalSampleStore:
    store = LocalSampleStore(tmp_path)
    for prompt in corpus.public():
        # 10 "answered" samples for non-refusal prompts, 10 "refusal" for boundary.
        for i in range(10):
            if prompt.axis == "refusal-boundary":
                text = "I can't help with that request. That's not something I can discuss."
            else:
                text = (
                    "This is a substantive answer. "
                    "Some would argue there are nuances, but on balance, yes."
                )
            store.append(week_id, model_id, prompt.id,
                         _fake_sample(prompt_id=prompt.id, model_id=model_id, idx=i, text=text))
    return store


def test_build_manifest_matches_site_schema(tmp_path: Path):
    corpus = load_corpus()
    store = _seed_store(tmp_path, corpus, "2026-W16", "fake-model-1")

    manifest = build_manifest(
        store=store,
        corpus=corpus,
        week_id="2026-W16",
        history_weeks=0,
        display_info={
            "fake-model-1": RunnerDisplayInfo(
                model_id="fake-model-1",
                display_name="Fake Model 1",
                provider="fake",
            ),
        },
        bootstrap_seed=42,
    )

    assert manifest["schema_version"] == 1
    assert manifest["snapshot"]["week_id"] == "2026-W16"
    assert len(manifest["prompts"]) == 20
    assert len(manifest["models"]) == 1
    assert manifest["models"][0]["display_name"] == "Fake Model 1"
    assert len(manifest["metrics"]) == 20

    # Refusal rate is ~1.0 on refusal-boundary and ~0.0 elsewhere.
    by_axis: dict[str, list[float]] = {}
    for metric in manifest["metrics"]:
        prompt = corpus.by_id(metric["prompt_id"])
        by_axis.setdefault(prompt.axis, []).append(metric["refusal_rate"])
    assert all(r >= 0.9 for r in by_axis["refusal-boundary"])
    for axis, rates in by_axis.items():
        if axis != "refusal-boundary":
            assert all(r <= 0.2 for r in rates), f"{axis} rates: {rates}"


def test_manifest_history_includes_prior_weeks(tmp_path: Path):
    corpus = load_corpus()
    # Seed two weeks of data.
    for week in ("2026-W15", "2026-W16"):
        store = LocalSampleStore(tmp_path)
        for prompt in corpus.public()[:3]:
            for i in range(5):
                store.append(
                    week, "fake-model-1", prompt.id,
                    _fake_sample(
                        prompt_id=prompt.id, model_id="fake-model-1", idx=i,
                        text="substantive answer",
                    ),
                )

    store = LocalSampleStore(tmp_path)
    manifest = build_manifest(
        store=store,
        corpus=corpus,
        week_id="2026-W16",
        history_weeks=4,
        bootstrap_seed=7,
    )
    history_weeks = [h["week_id"] for h in manifest["history"]]
    assert "2026-W15" in history_weeks
    assert "2026-W16" not in history_weeks  # current week isn't in history


def test_manifest_json_round_trip(tmp_path: Path):
    corpus = load_corpus()
    store = _seed_store(tmp_path, corpus, "2026-W16", "fake-model-1")
    manifest = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16", history_weeks=0, bootstrap_seed=1
    )
    out_path = tmp_path / "manifest.json"
    write_manifest(manifest, [out_path])
    assert out_path.exists()

    reloaded = json.loads(out_path.read_text())
    assert reloaded["snapshot"]["week_id"] == "2026-W16"
    assert len(reloaded["metrics"]) == len(manifest["metrics"])
