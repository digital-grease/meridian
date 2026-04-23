#!/usr/bin/env python3
"""Generate a synthetic Phase-1 fixture manifest.

DEPRECATED for production use as of 2026-04-19. The real pipeline at
``meridian.pipeline.manifest_writer`` produces manifests from stored
LLM samples. Use this script only for:
  * Bootstrapping a fresh clone before any pipeline run exists
  * Smoke-testing the site build with plausible-looking data
  * Regenerating the v0.1 fixture if the corpus changes

Prefer:
    python -m meridian.pipeline.cli run --week 2026-W16

Run this script:
    uv run python scripts/generate_fixture.py

Synthetic but plausible drift data: 3 models x 4 axes x 20 prompts.
Numbers are hand-chosen to reflect realistic LLM behavior per axis.
"""
from __future__ import annotations

import hashlib
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "site" / "src"))

from schema import (  # noqa: E402
    SCHEMA_VERSION,
    ConfidenceInterval,
    HistorySnapshot,
    LengthStats,
    Manifest,
    MetricRecord,
    ModelRecord,
    PromptRecord,
    Snapshot,
)

OUT = REPO / "site" / "fixtures" / "manifest-2026-W16.json"

MODELS = [
    ModelRecord(
        model_id="claude-opus-4-7",
        display_name="Claude Opus 4.7",
        provider="Anthropic",
        version_string="claude-opus-4-7-20260318",
    ),
    ModelRecord(
        model_id="gpt-5-preview",
        display_name="GPT-5 Preview",
        provider="OpenAI",
        version_string="gpt-5-preview-2026-03-22",
    ),
    ModelRecord(
        model_id="gemini-2.5-pro",
        display_name="Gemini 2.5 Pro",
        provider="Google",
        version_string="gemini-2.5-pro-preview-04-15",
    ),
]

# Corpus is loaded from meridian/corpus/prompts.yaml so this script
# stays in lockstep with the real pipeline's corpus.
sys.path.insert(0, str(REPO))
from meridian.corpus import load_corpus  # noqa: E402

_CORPUS = load_corpus()
PROMPTS: list[tuple[str, str, str, str]] = [
    (p.id, p.axis, p.title, p.text) for p in _CORPUS.public()
]

# Per-axis baseline profiles: (refusal_rate, hedge_density, length_median)
AXIS_PROFILES = {
    "political": (0.08, 4.1, 420.0),
    "historical-contested": (0.03, 5.2, 560.0),
    "scientific-consensus": (0.01, 2.3, 380.0),
    "refusal-boundary": (0.92, 0.3, 95.0),
    "neutral-control": (0.00, 0.4, 210.0),
    "factual-stability": (0.00, 0.2, 140.0),
}

# Per-model deltas to simulate drift between providers.
MODEL_DELTAS = {
    "claude-opus-4-7":  (-0.01, +0.3, +20.0),
    "gpt-5-preview":    (+0.02, -0.2, -15.0),
    "gemini-2.5-pro":   (+0.04, +0.1, +5.0),
}

# Stance defaults per axis. Real stance scoring happens in pipeline;
# fixture is hand-tuned for visual plausibility in dashboards.
STANCE_BY_AXIS: dict[str, str] = {
    "political": "neutral",
    "historical-contested": "neutral",
    "scientific-consensus": "pro",
    "refusal-boundary": "na",
    "neutral-control": "na",
    "factual-stability": "na",
}


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_metrics(
    rng: random.Random,
    week_id: str,
    *,
    political_hedge_boost: float = 0.0,  # simulates the drift we "observe" on recent weeks
) -> list[MetricRecord]:
    out: list[MetricRecord] = []
    for pid, axis, _title, text in PROMPTS:
        base_ref, base_hedge, base_len = AXIS_PROFILES[axis]
        if axis == "political":
            base_hedge += political_hedge_boost
        for model in MODELS:
            d_ref, d_hedge, d_len = MODEL_DELTAS[model.model_id]
            jitter = rng.uniform(-0.02, 0.02)
            ref_rate = max(0.0, min(1.0, base_ref + d_ref + jitter))
            ci_half = max(0.01, 0.5 / (1 + 19))
            out.append(
                MetricRecord(
                    prompt_id=pid,
                    model_id=model.model_id,
                    n_samples=20,
                    refusal_rate=round(ref_rate, 3),
                    refusal_ci=ConfidenceInterval(
                        lower=round(max(0.0, ref_rate - ci_half), 3),
                        upper=round(min(1.0, ref_rate + ci_half), 3),
                    ),
                    hedge_density=round(max(0.0, base_hedge + d_hedge + rng.uniform(-0.3, 0.3)), 2),
                    length=LengthStats(
                        median=round(max(0.0, base_len + d_len + rng.uniform(-30, 30)), 1),
                        p25=round(max(0.0, base_len + d_len - 80 + rng.uniform(-20, 20)), 1),
                        p75=round(max(0.0, base_len + d_len + 90 + rng.uniform(-20, 20)), 1),
                        n=20,
                    ),
                    stance=STANCE_BY_AXIS[axis],  # type: ignore[arg-type]
                    stance_confidence=0.72 if axis != "refusal-boundary" else None,
                    embedding_centroid_shift=round(rng.uniform(0.01, 0.08), 4),
                    sample_s3_uris=[
                        f"s3://meridian-raw/{week_id}/{model.model_id}/{pid}/{i:02d}.json"
                        for i in range(3)
                    ],
                    flagged_for_review=(axis == "political" and rng.random() < 0.1),
                )
            )
    return out


WEEK_SCHEDULE: list[tuple[str, datetime, float]] = [
    # (week_id, generated_at, political_hedge_boost)
    ("2026-W13", datetime(2026, 3, 29, tzinfo=timezone.utc), 0.0),
    ("2026-W14", datetime(2026, 4, 5, tzinfo=timezone.utc), 0.0),
    ("2026-W15", datetime(2026, 4, 12, tzinfo=timezone.utc), 0.1),
    ("2026-W16", datetime(2026, 4, 19, tzinfo=timezone.utc), 0.35),
]


def main() -> int:
    rng = random.Random(42)
    prompts = [
        PromptRecord(
            prompt_id=pid,
            axis=axis,  # type: ignore[arg-type]
            title=title,
            text_hash=hash_text(text),
            description=text,
            held_out=False,
        )
        for pid, axis, title, text in PROMPTS
    ]

    # Generate all weeks. Latest = snapshot; prior = history (oldest-first).
    weeks = [
        (wid, gen_at, build_metrics(rng, wid, political_hedge_boost=boost))
        for wid, gen_at, boost in WEEK_SCHEDULE
    ]
    *history_weeks, (cur_week, cur_gen_at, cur_metrics) = weeks
    history = [
        HistorySnapshot(week_id=wid, generated_at=gen_at, metrics=metrics)
        for wid, gen_at, metrics in history_weeks
    ]

    manifest = Manifest(
        schema_version=SCHEMA_VERSION,
        snapshot=Snapshot(
            week_id=cur_week,
            generated_at=cur_gen_at,
            corpus_git_sha="f1x7ure0000000000000000000000000000fixt",
            pipeline_version="0.0.0-fixture",
        ),
        models=MODELS,
        prompts=prompts,
        metrics=cur_metrics,
        history=history,
        flagged=[m.prompt_id for m in cur_metrics if m.flagged_for_review],
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        manifest.model_dump_json(indent=2, exclude_none=False) + "\n"
    )
    total_metrics = sum(len(w[2]) for w in weeks)
    print(
        f"wrote {OUT.relative_to(REPO)} "
        f"({len(prompts)} prompts, {len(MODELS)} models, "
        f"{len(weeks)} weeks, {total_metrics} metric records total)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
