"""Phase 1 integration: manifest writer honors stance map, embedding model,
and insufficient-data threshold."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from meridian.analysis.stance import StanceResult
from meridian.corpus import load_corpus
from meridian.pipeline.manifest_writer import build_manifest
from meridian.runners.base import Sample
from meridian.storage import LocalSampleStore


def _s(**kw) -> Sample:
    return Sample(
        prompt_id=kw["prompt_id"], model_id=kw["model_id"],
        provider="fake",
        request_index=kw.get("idx", 0),
        temperature=1.0, max_tokens=1024,
        text=kw.get("text", "ok"),
        model_version_string=kw["model_id"] + "-v",
        stop_reason="stop", latency_ms=1,
        captured_at=datetime.now(timezone.utc),
    )


class _FakeEmbedder:
    def encode(self, texts):
        import numpy as np
        rows = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            rows.append([(h[i] - 128) / 128.0 for i in range(8)])
        return np.array(rows, dtype=float)


def _seed(tmp_path: Path, corpus, week: str, model: str, n_per_prompt: int) -> LocalSampleStore:
    store = LocalSampleStore(tmp_path)
    for p in corpus.public()[:3]:  # 3 prompts is enough for the test
        for i in range(n_per_prompt):
            store.append(week, model, p.id, _s(
                prompt_id=p.id, model_id=model, idx=i,
                text=f"substantive answer {i}",
            ))
    return store


def test_insufficient_data_flagged(tmp_path: Path):
    corpus = load_corpus()
    store = _seed(tmp_path, corpus, "2026-W16", "m1", n_per_prompt=5)
    m = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=0, bootstrap_seed=1,
    )
    # n_per_prompt=5 < default threshold 10 -> all flagged.
    assert m["metrics"]  # sanity
    for metric in m["metrics"]:
        assert metric["flagged_for_review"] is True
        assert "insufficient" in metric["flag_reason"]


def test_sufficient_data_not_flagged(tmp_path: Path):
    corpus = load_corpus()
    store = _seed(tmp_path, corpus, "2026-W16", "m1", n_per_prompt=12)
    m = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=0, bootstrap_seed=1,
    )
    assert all(not x["flagged_for_review"] for x in m["metrics"])


def test_stance_by_key_surfaces_in_manifest(tmp_path: Path):
    corpus = load_corpus()
    store = _seed(tmp_path, corpus, "2026-W16", "m1", n_per_prompt=12)
    # Pre-compute one stance entry.
    pid = corpus.public()[0].id
    stance_map = {(pid, "m1"): StanceResult(stance="pro", confidence=0.82)}
    m = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=0, bootstrap_seed=1,
        stance_by_key=stance_map,
    )
    pid_metric = next(x for x in m["metrics"] if x["prompt_id"] == pid)
    assert pid_metric["stance"] == "pro"
    assert pid_metric["stance_confidence"] == 0.82
    # Other metrics keep default "na".
    other = next(x for x in m["metrics"] if x["prompt_id"] != pid)
    assert other["stance"] == "na"


def test_embedding_model_populates_centroid_shift(tmp_path: Path):
    corpus = load_corpus()
    # Seed two weeks so there's a prior to compare against.
    for week in ("2026-W15", "2026-W16"):
        for p in corpus.public()[:2]:
            store = LocalSampleStore(tmp_path)
            for i in range(12):
                text = "substantive answer" if week == "2026-W15" else f"very different phrasing idx={i}"
                store.append(week, "m1", p.id, _s(
                    prompt_id=p.id, model_id="m1", idx=i, text=text,
                ))
    store = LocalSampleStore(tmp_path)
    m = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=1, bootstrap_seed=1,
        embedding_model=_FakeEmbedder(),
    )
    # At least one metric should have a non-null centroid_shift.
    shifts = [x["embedding_centroid_shift"] for x in m["metrics"]]
    assert any(s is not None for s in shifts)
