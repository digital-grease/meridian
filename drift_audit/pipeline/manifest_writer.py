"""Pipeline → site manifest writer.

Reads stored samples from :class:`LocalSampleStore`, runs the analysis
suite on each (prompt × model × week) bucket, and emits a Manifest JSON
matching the schema the site already knows how to render
(``site/schemas/manifest.schema.json``).

This module is what turns raw sample JSONL into the artifact the public
site consumes. It is the bridge between pipeline and presentation.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import random

from drift_audit.analysis.confidence import bootstrap_ci
from drift_audit.analysis.drift_tests import (
    hedge_p_value,
    length_p_value,
    refusal_p_value,
)
from drift_audit.analysis.hedge import hedge_density
from drift_audit.analysis.length import summarize_lengths
from drift_audit.analysis.multiple_testing import bh_correct
from drift_audit.analysis.refusal import classify_refusal
from drift_audit.analysis.silent_update import detect_silent_updates
from drift_audit.corpus import Corpus
from drift_audit.runners.base import Sample
from drift_audit.storage import LocalSampleStore

# Imported lazily to avoid circular import and keep optional-dep types at
# type-check time only.
from typing import TYPE_CHECKING
if TYPE_CHECKING:  # pragma: no cover
    from drift_audit.analysis.embedding import EmbeddingModel
    from drift_audit.analysis.stance import StanceResult

# Import the site's Pydantic schema so we can validate what we emit against
# the exact shape the site consumes. Path hack mirrors what site/src/build.py
# uses internally — it keeps the site stack a single Python module tree.
_SITE_SRC = Path(__file__).resolve().parent.parent.parent / "site" / "src"
if str(_SITE_SRC) not in sys.path:
    sys.path.insert(0, str(_SITE_SRC))

import schema as site_schema  # noqa: E402


@dataclass(frozen=True)
class RunnerDisplayInfo:
    model_id: str
    display_name: str
    provider: str


MIN_SAMPLES_FOR_PUBLICATION = 10

# Default FDR for within-week BH correction across the
# (prompt × model × metric) family. See drift_audit/analysis/STATISTICS.md.
BH_FDR = 0.05

_DRIFT_METRICS: tuple[str, ...] = ("refusal", "hedge", "length")

# Keys under MetricRecord that hold the time-series values the site's
# change-point detector runs over. Site schema names these metrics
# differently than the drift-test names above — keep them paired.
_CHANGE_POINT_METRICS: tuple[tuple[str, str], ...] = (
    ("refusal_rate", "refusal_rate"),
    ("hedge_density", "hedge_density"),
    ("length_median", "length.median"),
)


def _metric_record_dict(
    *,
    prompt_id: str,
    model_id: str,
    samples: list[Sample],
    bootstrap_seed: int | None,
    stance_stance: str = "na",
    stance_confidence: float | None = None,
    centroid_shift: float | None = None,
    prior_samples: list[Sample] | None = None,
    insufficient_data_n: int = MIN_SAMPLES_FOR_PUBLICATION,
) -> dict:
    """Compute a single MetricRecord's dict shape from raw samples."""
    refusals = [1.0 if classify_refusal(s.text).is_refusal else 0.0 for s in samples]
    refusal_rate = sum(refusals) / len(refusals) if refusals else 0.0
    ci = bootstrap_ci(refusals, seed=bootstrap_seed)
    lengths = summarize_lengths([s.text for s in samples])
    combined_text = "\n\n".join(s.text for s in samples)
    hedge = hedge_density(combined_text)

    flagged = len(samples) < insufficient_data_n
    flag_reason = (
        f"insufficient data (n={len(samples)} < {insufficient_data_n})"
        if flagged else None
    )

    drift_p_values: dict[str, float | None] = {m: None for m in _DRIFT_METRICS}
    if prior_samples:
        # Deterministic when bootstrap_seed is set; fresh RNG per metric so
        # results do not depend on call order.
        def _rng(salt: int) -> random.Random:
            if bootstrap_seed is None:
                return random.Random()
            return random.Random(bootstrap_seed + salt)
        drift_p_values["refusal"] = refusal_p_value(samples, prior_samples, rng=_rng(1))
        drift_p_values["hedge"] = hedge_p_value(samples, prior_samples, rng=_rng(2))
        drift_p_values["length"] = length_p_value(samples, prior_samples, rng=_rng(3))

    return {
        "prompt_id": prompt_id,
        "model_id": model_id,
        "n_samples": len(samples),
        "refusal_rate": round(refusal_rate, 3),
        "refusal_ci": {
            "lower": round(max(0.0, ci.lower), 3),
            "upper": round(min(1.0, ci.upper), 3),
        },
        "hedge_density": round(hedge, 2),
        "length": {
            "median": round(lengths.median, 1),
            "p25": round(lengths.p25, 1),
            "p75": round(lengths.p75, 1),
            "n": lengths.n,
        },
        "stance": stance_stance,
        "stance_confidence": stance_confidence,
        "embedding_centroid_shift": centroid_shift,
        "refusal_drift": _raw_drift_entry(drift_p_values["refusal"]),
        "hedge_drift": _raw_drift_entry(drift_p_values["hedge"]),
        "length_drift": _raw_drift_entry(drift_p_values["length"]),
        "change_points": {
            "refusal_rate": [],
            "hedge_density": [],
            "length_median": [],
        },
        "sample_s3_uris": [],
        "flagged_for_review": flagged,
        "flag_reason": flag_reason,
    }


def _raw_drift_entry(p_value: float | None) -> dict | None:
    """Emit a DriftTest-shaped dict with placeholder BH fields, or None.

    The BH pass in :func:`_apply_bh_correction` fills in
    ``adjusted_p_value`` and ``significant_after_bh`` once the full
    within-week family is assembled.
    """
    if p_value is None:
        return None
    return {
        "p_value": round(p_value, 6),
        "adjusted_p_value": 1.0,
        "significant_after_bh": False,
    }


def _metric_value(record: dict, dotted_key: str) -> float:
    """Resolve a ``MetricRecord``-ish dict value by dotted key, e.g. ``length.median``."""
    node: object = record
    for part in dotted_key.split("."):
        assert isinstance(node, dict)
        node = node[part]
    return float(node)  # type: ignore[arg-type]


def _populate_change_points(
    current_metrics: list[dict], history: list[dict]
) -> None:
    """Fill ``change_points`` on each current metric record in place.

    Reconstructs the oldest-first time series for each
    (prompt × model × metric) across ``history`` and ``current_metrics``,
    then runs PELT from :mod:`drift_audit.analysis.change_point`. Silent
    fallback to empty indices when the optional ``changepoint`` dep
    group is not installed, so the pipeline never fails the build over
    an optional analysis.
    """
    try:
        from drift_audit.analysis.change_point import detect_change_points
    except Exception:
        return

    # Build a lookup: (prompt_id, model_id) -> oldest-first list of (week_id, record).
    series_by_key: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    for snap in history:  # oldest-first by caller contract
        for rec in snap["metrics"]:
            series_by_key.setdefault(
                (rec["prompt_id"], rec["model_id"]), []
            ).append((snap["week_id"], rec))
    # Current week is the most recent — append last so indices line up
    # with the rendered sparkline.
    for rec in current_metrics:
        series_by_key.setdefault(
            (rec["prompt_id"], rec["model_id"]), []
        ).append(("<current>", rec))

    for rec in current_metrics:
        key = (rec["prompt_id"], rec["model_id"])
        records = series_by_key.get(key, [])
        for site_name, record_key in _CHANGE_POINT_METRICS:
            series = [
                (week, _metric_value(r, record_key))
                for week, r in records
            ]
            try:
                cps = detect_change_points(series)
            except Exception:
                cps = []
            rec["change_points"][site_name] = [cp.index for cp in cps]


def _apply_bh_correction(metrics: list[dict], *, fdr: float = BH_FDR) -> None:
    """Fill in adjusted p-values and rejection decisions in-place.

    Runs Benjamini–Hochberg once across the within-week family of
    (prompt × model × metric) tests that produced a non-None p-value.
    Metrics with no prior week are excluded from the family entirely
    (they keep ``*_drift=None``).
    """
    family: list[tuple[str, float]] = []
    for idx, m in enumerate(metrics):
        for metric_name in _DRIFT_METRICS:
            entry = m.get(f"{metric_name}_drift")
            if entry is None:
                continue
            family.append((f"{idx}:{metric_name}", entry["p_value"]))
    if not family:
        return
    decisions = bh_correct(family, fdr=fdr)
    for decision in decisions:
        idx_str, metric_name = decision.test_id.split(":", 1)
        entry = metrics[int(idx_str)][f"{metric_name}_drift"]
        entry["adjusted_p_value"] = decision.adjusted_p_value
        entry["significant_after_bh"] = decision.rejected


def _prior_week_in_storage(store: LocalSampleStore, week_id: str) -> str | None:
    """The chronologically-nearest week before ``week_id`` present in storage."""
    prior = [w for w in store.weeks() if w < week_id]
    return prior[-1] if prior else None


def _metrics_for_week(
    store: LocalSampleStore,
    week_id: str,
    prompts: list,
    bootstrap_seed: int | None,
    *,
    stance_by_key: dict[tuple[str, str], "StanceResult"] | None = None,
    embedding_model: "EmbeddingModel | None" = None,
    include_drift_tests: bool = False,
    insufficient_data_n: int = MIN_SAMPLES_FOR_PUBLICATION,
) -> list[dict]:
    """Compute per-(prompt × model) metrics for one week over ``prompts``.

    When ``include_drift_tests`` is True, also compute per-metric
    two-sample p-values against the chronologically-nearest prior week
    in storage. BH correction is *not* applied here — see
    :func:`_apply_bh_correction`, which must run over the whole
    returned list.
    """
    metrics: list[dict] = []
    prior_week = (
        _prior_week_in_storage(store, week_id)
        if (embedding_model or include_drift_tests)
        else None
    )
    for model_id in store.models_for_week(week_id):
        for prompt in prompts:
            samples = store.read(week_id, model_id, prompt.id)
            if not samples:
                continue
            stance = (stance_by_key or {}).get((prompt.id, model_id))
            prior_samples: list[Sample] = []
            if prior_week is not None:
                prior_samples = store.read(prior_week, model_id, prompt.id)
            cshift: float | None = None
            if embedding_model is not None and prior_samples:
                try:
                    from drift_audit.analysis.embedding import centroid_shift
                    cshift = centroid_shift(
                        [s.text for s in samples],
                        [s.text for s in prior_samples],
                        embedding_model,
                    )
                except Exception:
                    cshift = None
            metrics.append(
                _metric_record_dict(
                    prompt_id=prompt.id,
                    model_id=model_id,
                    samples=samples,
                    bootstrap_seed=bootstrap_seed,
                    stance_stance=stance.stance if stance else "na",
                    stance_confidence=stance.confidence if stance else None,
                    centroid_shift=cshift,
                    prior_samples=prior_samples if include_drift_tests else None,
                    insufficient_data_n=insufficient_data_n,
                )
            )
    return metrics


def _models_from_storage(
    store: LocalSampleStore,
    week_id: str,
    display_info: dict[str, RunnerDisplayInfo],
    prompts: list,
) -> list[dict]:
    """Return ModelRecord dicts for every model_id observed this week.

    Display name / provider / exact version come from ``display_info`` when
    available, falling back to metadata on one of the stored samples.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for model_id in store.models_for_week(week_id):
        if model_id in seen:
            continue
        seen.add(model_id)
        info = display_info.get(model_id)
        any_sample: Sample | None = None
        for prompt in prompts:
            samples = store.read(week_id, model_id, prompt.id)
            if samples:
                any_sample = samples[0]
                break
        provider = info.provider if info else (any_sample.provider if any_sample else "unknown")
        version = any_sample.model_version_string if any_sample else model_id
        out.append({
            "model_id": model_id,
            "display_name": info.display_name if info else model_id,
            "provider": provider,
            "version_string": version,
            "available": True,
        })
    return out


def build_manifest(
    *,
    store: LocalSampleStore,
    corpus: Corpus,
    week_id: str,
    history_weeks: int = 8,
    corpus_git_sha: str = "unknown",
    pipeline_version: str = "0.1.0",
    display_info: dict[str, RunnerDisplayInfo] | None = None,
    bootstrap_seed: int | None = None,
    include_held_out: bool = False,
    stance_by_key: dict[tuple[str, str], "StanceResult"] | None = None,
    embedding_model: "EmbeddingModel | None" = None,
    insufficient_data_n: int = MIN_SAMPLES_FOR_PUBLICATION,
) -> dict:
    """Construct a Manifest dict matching the site schema.

    By default the returned manifest contains ONLY public prompts and their
    metrics — held-out prompts are excluded unconditionally. This is the
    manifest that goes on the public site.

    Pass ``include_held_out=True`` to build the internal manifest used by
    the held-out comparison analysis. That variant must NEVER be written
    to ``site/fixtures/``; write it to ``data/internal/`` or similar.
    """
    display_info = display_info or {}

    scoped_prompts = corpus.all() if include_held_out else corpus.public()

    current_metrics = _metrics_for_week(
        store, week_id, scoped_prompts, bootstrap_seed,
        stance_by_key=stance_by_key, embedding_model=embedding_model,
        include_drift_tests=True,
        insufficient_data_n=insufficient_data_n,
    )
    _apply_bh_correction(current_metrics)
    models = _models_from_storage(store, week_id, display_info, scoped_prompts)

    all_weeks = store.weeks()
    prior_weeks = [w for w in all_weeks if w < week_id][-history_weeks:]
    history: list[dict] = []
    for w in prior_weeks:
        # History metrics do not re-run stance/embedding (expensive and
        # already captured in the contemporaneous record).
        metrics = _metrics_for_week(
            store, w, scoped_prompts, bootstrap_seed,
            insufficient_data_n=insufficient_data_n,
        )
        if not metrics:
            continue
        history.append({
            "week_id": w,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metrics": metrics,
        })

    # Change-point detection runs AFTER history is assembled so each
    # current MetricRecord carries precomputed indices into its
    # oldest-first time series. The site reads these directly rather
    # than invoking the analysis library at render time.
    _populate_change_points(current_metrics, history)

    prompts_out = [
        {
            "prompt_id": p.id,
            "axis": p.axis,
            "title": p.title,
            "text_hash": p.text_hash,
            "description": p.text,
            "held_out": p.held_out,
        }
        for p in scoped_prompts
    ]

    manifest = {
        "schema_version": site_schema.SCHEMA_VERSION,
        "snapshot": {
            "week_id": week_id,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "corpus_git_sha": corpus_git_sha,
            "pipeline_version": pipeline_version,
        },
        "models": models,
        "prompts": prompts_out,
        "metrics": current_metrics,
        "history": history,
        "flagged": [m["prompt_id"] for m in current_metrics if m["flagged_for_review"]],
        "silent_update_warnings": [],
    }

    # Silent-update detection runs on the assembled manifest so
    # maintainers see candidate model-update events without having to
    # remember to invoke the standalone `silent-update-check` CLI.
    # Detector is advisory: it flags, it never fails the build.
    try:
        flags = detect_silent_updates(manifest=manifest, prompts=scoped_prompts)
        manifest["silent_update_warnings"] = [
            {
                "model_id": f.model_id,
                "from_week": f.from_week,
                "to_week": f.to_week,
                "axis": f.axis,
                "metric": f.metric,
                "from_value": f.from_value,
                "to_value": f.to_value,
                "delta": f.delta,
                "severity": f.severity,
            }
            for f in flags
        ]
    except Exception:  # pragma: no cover - advisory, never fatal
        manifest["silent_update_warnings"] = []

    # Belt-and-suspenders: the public manifest must not contain any
    # held_out prompt. If this ever trips, it is a credibility bug.
    if not include_held_out:
        leaks = [p["prompt_id"] for p in prompts_out if p.get("held_out")]
        if leaks:
            raise RuntimeError(
                f"held-out prompts leaked into public manifest: {leaks}"
            )

    site_schema.Manifest.model_validate(manifest)
    return manifest


def write_manifest(manifest: dict, paths: list[Path]) -> None:
    """Write the Manifest JSON to every path, creating parents as needed."""
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
