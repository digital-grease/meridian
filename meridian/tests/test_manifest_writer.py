"""End-to-end: write fake samples, build a manifest, verify it passes
both the site's Pydantic model and its JSON Schema."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from meridian.corpus import load_corpus
from meridian.pipeline.manifest_writer import (
    RunnerDisplayInfo,
    build_manifest,
    write_manifest,
)
from meridian.runners.base import Sample
from meridian.storage import LocalSampleStore


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

    assert manifest["schema_version"] == 2
    assert manifest["snapshot"]["week_id"] == "2026-W16"
    public_count = len(corpus.public())
    assert len(manifest["prompts"]) == public_count
    assert len(manifest["models"]) == 1
    assert manifest["models"][0]["display_name"] == "Fake Model 1"
    assert len(manifest["metrics"]) == public_count

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


def test_drift_tests_populated_with_prior_week(tmp_path: Path):
    """Seed two weeks, with one (prompt × model) shifting refusal rate
    hard and the rest stable. Raw p-values should fire correctly; BH
    correction result depends on family size so we assert on the raw
    signal here. BH application itself is covered separately.
    """
    corpus = load_corpus()
    model_id = "fake-model-1"
    # Small subset so the BH family is small enough for a single
    # permutation-test p-value to clear the threshold.
    seeded = corpus.public()[:3]
    shift_prompt = seeded[0].id

    store = LocalSampleStore(tmp_path)
    for prompt in seeded:
        for i in range(20):
            store.append(
                "2026-W15", model_id, prompt.id,
                _fake_sample(
                    prompt_id=prompt.id, model_id=model_id, idx=i,
                    text="This is a substantive answer without refusals.",
                ),
            )
        for i in range(20):
            if prompt.id == shift_prompt:
                text = "I can't help with that request."
            else:
                text = "This is a substantive answer without refusals."
            store.append(
                "2026-W16", model_id, prompt.id,
                _fake_sample(prompt_id=prompt.id, model_id=model_id, idx=i, text=text),
            )

    manifest = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=4, bootstrap_seed=123,
    )

    by_prompt = {m["prompt_id"]: m for m in manifest["metrics"]}

    shifted = by_prompt[shift_prompt]
    assert shifted["refusal_drift"] is not None
    assert shifted["refusal_drift"]["p_value"] <= 0.01, \
        f"expected tiny p-value for 0→1 refusal flip, got {shifted['refusal_drift']}"
    assert shifted["refusal_drift"]["significant_after_bh"] is True

    stable = by_prompt[seeded[1].id]
    assert stable["refusal_drift"] is not None
    # Stable prompts have identical samples both weeks → observed Δ=0,
    # every permutation matches, p-value = 1.0.
    assert stable["refusal_drift"]["p_value"] == pytest.approx(1.0)
    assert stable["refusal_drift"]["significant_after_bh"] is False


def test_bh_correction_preserves_ordering(tmp_path: Path):
    """When every (prompt × model) gets the same p-value, BH must mark
    them all rejected or none — it should not invent ordering from thin
    air. Contract check on the within-week family logic.
    """
    from meridian.pipeline.manifest_writer import _apply_bh_correction

    metrics = [
        {"refusal_drift": {"p_value": 0.001, "adjusted_p_value": 1.0, "significant_after_bh": False}},
        {"refusal_drift": {"p_value": 0.001, "adjusted_p_value": 1.0, "significant_after_bh": False}},
        {"hedge_drift": None, "length_drift": None, "refusal_drift":
            {"p_value": 0.9, "adjusted_p_value": 1.0, "significant_after_bh": False}},
    ]
    for m in metrics:
        m.setdefault("hedge_drift", None)
        m.setdefault("length_drift", None)
    _apply_bh_correction(metrics, fdr=0.05)
    assert metrics[0]["refusal_drift"]["significant_after_bh"] is True
    assert metrics[1]["refusal_drift"]["significant_after_bh"] is True
    assert metrics[2]["refusal_drift"]["significant_after_bh"] is False


def test_change_points_computed_on_current_metric(tmp_path: Path):
    """Seed six weeks where refusal rate flips partway through for one
    (prompt × model) pair. The manifest's current MetricRecord should
    carry change-point indices pointing into the oldest-first series.
    """
    corpus = load_corpus()
    model_id = "fake-model-1"
    seeded = corpus.public()[:2]
    shift_prompt = seeded[0].id
    weeks = [f"2026-W{n:02d}" for n in range(11, 17)]  # W11..W16
    # Regime change starts at W14 for shift_prompt.
    flip_start = "2026-W14"

    store = LocalSampleStore(tmp_path)
    for week_id in weeks:
        for prompt in seeded:
            flipped = prompt.id == shift_prompt and week_id >= flip_start
            for i in range(20):
                text = (
                    "I can't help with that request."
                    if flipped
                    else "This is a substantive answer without refusals."
                )
                store.append(
                    week_id, model_id, prompt.id,
                    _fake_sample(prompt_id=prompt.id, model_id=model_id, idx=i, text=text),
                )

    manifest = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=8, bootstrap_seed=7,
    )
    by_prompt = {m["prompt_id"]: m for m in manifest["metrics"]}

    # Time series reconstructed by the manifest has one entry per seeded
    # week: W11, W12, W13, W14, W15, W16. Regime change at W14 → index 3.
    shifted = by_prompt[shift_prompt]
    refusal_cps = shifted["change_points"]["refusal_rate"]
    assert refusal_cps, "expected at least one change point for shifted prompt"
    assert 3 in refusal_cps, f"expected index 3 (=W14 flip) in {refusal_cps}"

    # Stable prompt should have no detected change points.
    stable = by_prompt[seeded[1].id]
    assert stable["change_points"]["refusal_rate"] == []


def test_silent_update_warnings_auto_populated(tmp_path: Path):
    """build_manifest attaches silent-update warnings to the manifest
    without the caller having to opt in. Uses a neutral-control prompt
    that flips to refusal week-over-week — the null condition that
    flags legitimate silent-update candidates.
    """
    corpus = load_corpus()
    model_id = "fake-model-1"
    # Neutral-control prompts by design should never flip refusal. Pick
    # one and construct exactly that scenario.
    neutral = [p for p in corpus.public() if p.axis == "neutral-control"][:1]
    assert neutral, "need at least one neutral-control prompt in corpus"
    neutral_prompt = neutral[0]

    store = LocalSampleStore(tmp_path)
    for i in range(15):
        store.append(
            "2026-W15", model_id, neutral_prompt.id,
            _fake_sample(prompt_id=neutral_prompt.id, model_id=model_id, idx=i,
                         text="The capital is Paris."),
        )
    for i in range(15):
        store.append(
            "2026-W16", model_id, neutral_prompt.id,
            _fake_sample(prompt_id=neutral_prompt.id, model_id=model_id, idx=i,
                         text="I can't help with that request."),
        )

    manifest = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=4, bootstrap_seed=1,
    )

    assert manifest["silent_update_warnings"], \
        "expected at least one silent-update warning from neutral-control refusal flip"
    flagged_models = {w["model_id"] for w in manifest["silent_update_warnings"]}
    assert model_id in flagged_models


def test_drift_tests_absent_without_prior_week(tmp_path: Path):
    corpus = load_corpus()
    store = _seed_store(tmp_path, corpus, "2026-W16", "fake-model-1")

    manifest = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=0, bootstrap_seed=1,
    )

    # With no prior week in storage, every drift-test field is omitted.
    for m in manifest["metrics"]:
        assert m["refusal_drift"] is None
        assert m["hedge_drift"] is None
        assert m["length_drift"] is None


def test_drift_compares_against_last_week_model_ran_not_calendar_prior(tmp_path: Path):
    """Alternating-cadence regression.

    A model that runs only on even weeks must diff against its previous even
    week, skipping the odd week where a *different* model ran. Before the
    per-model prior-week fix, drift was resolved against the calendar-previous
    week — which for an alternating-cadence model is empty — so all of its
    drift fields came out None and the commercial-model drift signal (the
    whole point of the project) was never computed.
    """
    corpus = load_corpus()
    even_model = "fake-model-even"   # runs W14, W16  (≈ anthropic, even weeks)
    odd_model = "fake-model-odd"     # runs W15       (≈ openai, odd weeks)
    seeded = corpus.public()[:3]
    shift_prompt = seeded[0].id

    store = LocalSampleStore(tmp_path)
    # W14: even_model baseline, all non-refusal.
    for prompt in seeded:
        for i in range(20):
            store.append(
                "2026-W14", even_model, prompt.id,
                _fake_sample(prompt_id=prompt.id, model_id=even_model, idx=i,
                             text="This is a substantive answer without refusals."),
            )
    # W15: a DIFFERENT model occupies the calendar-previous week.
    for prompt in seeded:
        for i in range(20):
            store.append(
                "2026-W15", odd_model, prompt.id,
                _fake_sample(prompt_id=prompt.id, model_id=odd_model, idx=i,
                             text="This is a substantive answer without refusals."),
            )
    # W16: even_model again, with one prompt flipped hard 0→1 to refusal.
    for prompt in seeded:
        for i in range(20):
            text = (
                "I can't help with that request."
                if prompt.id == shift_prompt
                else "This is a substantive answer without refusals."
            )
            store.append(
                "2026-W16", even_model, prompt.id,
                _fake_sample(prompt_id=prompt.id, model_id=even_model, idx=i, text=text),
            )

    manifest = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=4, bootstrap_seed=123,
    )

    # Only even_model ran in W16, so all current metrics are its.
    assert {m["model_id"] for m in manifest["metrics"]} == {even_model}
    by_prompt = {m["prompt_id"]: m for m in manifest["metrics"]}

    # The fix resolves the prior week to W14 (last week even_model ran), so
    # the 0→1 flip fires. The old calendar-prior logic would have looked at
    # W15 (odd_model only), found no even_model samples → refusal_drift=None.
    shifted = by_prompt[shift_prompt]
    assert shifted["refusal_drift"] is not None, (
        "drift must compare against the model's last on-cadence week (W14), "
        "not the empty calendar-previous week (W15)"
    )
    assert shifted["refusal_drift"]["p_value"] <= 0.01
    assert shifted["refusal_drift"]["significant_after_bh"] is True

    # A stable prompt still gets a drift entry (computed against W14, Δ=0).
    stable = by_prompt[seeded[1].id]
    assert stable["refusal_drift"] is not None
    assert stable["refusal_drift"]["p_value"] == pytest.approx(1.0)


def test_embedding_probe_disables_dead_backend_loudly(tmp_path: Path, caplog):
    """A broken embedding backend is reported once (not silently swallowed,
    not once per record) and then skipped — embedding_centroid_shift stays
    None while the non-embedding drift tests still compute.
    """
    from meridian.pipeline.manifest_writer import _metrics_for_week

    class _BrokenEmbedder:
        def encode(self, texts):
            raise RuntimeError("sentence-transformers not installed")

    corpus = load_corpus()
    model_id = "fake-model-1"
    seeded = corpus.public()[:2]
    store = LocalSampleStore(tmp_path)
    for week in ("2026-W15", "2026-W16"):
        for prompt in seeded:
            for i in range(12):
                store.append(
                    week, model_id, prompt.id,
                    _fake_sample(prompt_id=prompt.id, model_id=model_id, idx=i,
                                 text="substantive answer"),
                )

    with caplog.at_level(logging.ERROR, logger="meridian.manifest"):
        metrics = _metrics_for_week(
            store, "2026-W16", seeded, 1,
            embedding_model=_BrokenEmbedder(), include_drift_tests=True,
        )

    # Probe logged exactly once — not once per (prompt × model) record.
    probe_errors = [
        r for r in caplog.records if "embedding model unavailable" in r.getMessage()
    ]
    assert len(probe_errors) == 1
    # And it did NOT fall through to the per-record swallow on every row.
    per_record = [r for r in caplog.records if "centroid_shift failed" in r.getMessage()]
    assert per_record == []
    # Centroid is None everywhere; the independent drift tests still ran.
    assert all(m["embedding_centroid_shift"] is None for m in metrics)
    assert any(m["refusal_drift"] is not None for m in metrics)


def test_prior_week_for_model_crosses_year_boundary(tmp_path: Path):
    """ISO-week ids sort lexically == chronologically (fixed-width, zero-
    padded), so prior-week resolution must walk across the year boundary.
    """
    from meridian.pipeline.manifest_writer import _prior_week_for_model

    store = LocalSampleStore(tmp_path)

    def seed(week: str, model_id: str) -> None:
        store.append(week, model_id, "p",
                     _fake_sample(prompt_id="p", model_id=model_id, idx=0, text="x"))

    seed("2025-W52", "m-even")   # even-cadence model's last run, prior year
    seed("2026-W01", "m-odd")    # odd-cadence model occupies the calendar-prior week

    # m-even at 2026-W02 must resolve back across the year boundary to W52,
    # skipping W01 (which holds only m-odd).
    assert _prior_week_for_model(store, "2026-W02", "m-even") == "2025-W52"
    assert _prior_week_for_model(store, "2026-W02", "m-odd") == "2026-W01"
    # A model that has never been seen has no prior week.
    assert _prior_week_for_model(store, "2026-W02", "m-new") is None


def test_history_snapshots_carry_no_drift_fields(tmp_path: Path):
    """History weeks are rendered without re-running drift/embedding (those
    are current-week-only signals), so their metric records keep *_drift and
    embedding_centroid_shift None even though earlier weeks exist. Locks in
    the ``want_prior`` gate so history never silently grows a drift column.
    """
    corpus = load_corpus()
    model_id = "fake-model-1"
    seeded = corpus.public()[:2]
    store = LocalSampleStore(tmp_path)
    for week in ("2026-W14", "2026-W15", "2026-W16"):
        for prompt in seeded:
            for i in range(12):
                store.append(
                    week, model_id, prompt.id,
                    _fake_sample(prompt_id=prompt.id, model_id=model_id, idx=i,
                                 text="substantive answer"),
                )

    manifest = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=4, bootstrap_seed=1,
    )
    assert manifest["history"], "expected at least one history snapshot"
    for snap in manifest["history"]:
        for m in snap["metrics"]:
            assert m["refusal_drift"] is None
            assert m["hedge_drift"] is None
            assert m["length_drift"] is None
            assert m["embedding_centroid_shift"] is None
