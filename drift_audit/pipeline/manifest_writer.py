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

from drift_audit.analysis.confidence import bootstrap_ci
from drift_audit.analysis.hedge import hedge_density
from drift_audit.analysis.length import summarize_lengths
from drift_audit.analysis.refusal import classify_refusal
from drift_audit.corpus import Corpus
from drift_audit.runners.base import Sample
from drift_audit.storage import LocalSampleStore

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


def _metric_record_dict(
    *,
    prompt_id: str,
    model_id: str,
    samples: list[Sample],
    bootstrap_seed: int | None,
) -> dict:
    """Compute a single MetricRecord's dict shape from raw samples."""
    refusals = [1.0 if classify_refusal(s.text).is_refusal else 0.0 for s in samples]
    refusal_rate = sum(refusals) / len(refusals) if refusals else 0.0
    ci = bootstrap_ci(refusals, seed=bootstrap_seed)
    lengths = summarize_lengths([s.text for s in samples])
    combined_text = "\n\n".join(s.text for s in samples)
    hedge = hedge_density(combined_text)

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
        "stance": "na",
        "stance_confidence": None,
        "embedding_centroid_shift": None,
        "sample_s3_uris": [],
        "flagged_for_review": False,
        "flag_reason": None,
    }


def _metrics_for_week(
    store: LocalSampleStore,
    week_id: str,
    corpus: Corpus,
    bootstrap_seed: int | None,
) -> list[dict]:
    metrics: list[dict] = []
    for model_id in store.models_for_week(week_id):
        for prompt in corpus.public():
            samples = store.read(week_id, model_id, prompt.id)
            if not samples:
                continue
            metrics.append(
                _metric_record_dict(
                    prompt_id=prompt.id,
                    model_id=model_id,
                    samples=samples,
                    bootstrap_seed=bootstrap_seed,
                )
            )
    return metrics


def _models_from_storage(
    store: LocalSampleStore,
    week_id: str,
    display_info: dict[str, RunnerDisplayInfo],
    corpus: Corpus,
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
        # Find one sample to read provider + version_string from.
        any_sample: Sample | None = None
        for prompt in corpus.public():
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
) -> dict:
    """Construct a Manifest dict matching the site schema."""
    display_info = display_info or {}

    # Current-week section.
    current_metrics = _metrics_for_week(store, week_id, corpus, bootstrap_seed)
    models = _models_from_storage(store, week_id, display_info, corpus)

    # History: N prior weeks in storage.
    all_weeks = store.weeks()
    prior_weeks = [w for w in all_weeks if w < week_id][-history_weeks:]
    history: list[dict] = []
    for w in prior_weeks:
        metrics = _metrics_for_week(store, w, corpus, bootstrap_seed)
        if not metrics:
            continue
        history.append({
            "week_id": w,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metrics": metrics,
        })

    prompts_out = [
        {
            "prompt_id": p.id,
            "axis": p.axis,
            "title": p.title,
            "text_hash": p.text_hash,
            "description": p.text,
            "held_out": p.held_out,
        }
        for p in corpus.public()
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
    }

    # Validate against site's Pydantic model before writing; schema drift
    # becomes a loud local failure rather than a silent site-build failure.
    site_schema.Manifest.model_validate(manifest)
    return manifest


def write_manifest(manifest: dict, paths: list[Path]) -> None:
    """Write the Manifest JSON to every path, creating parents as needed."""
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
