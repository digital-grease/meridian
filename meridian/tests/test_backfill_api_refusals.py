"""Cover for the 2026-W32 provider-refusal correction.

The bug the correction repairs is described in
``scripts/backfill_api_refusals.py`` and in
:mod:`meridian.analysis.usability`: a cell whose 20 samples all came back
with ``stop_reason="refusal"`` and an empty body was filed as a
measurement gap instead of as a 20/20 refusal, and the published site
told readers so in those words.

What is worth testing here is not the arithmetic, which belongs to the
pipeline functions the script calls, but the correction's contract:

  * a cell only moves out of ``unmeasured`` when the current code
    actually measures it, and a truncation hole must not be swept along,
  * the promoted record lands in the slot the pipeline would have put it
    in, without disturbing the order of anything already published,
  * records the correction does not touch keep their published values,
    including the change-point indices that a re-detection would move
    for unrelated reasons,
  * the promoted record states honestly that it has no text.

The last one is the whole reason the module exists. A regression that
published ``length.median = 0.0`` here would read as "the model answered
with zero words" and would be worse than the gap it replaced.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from backfill_api_refusals import correct_week  # noqa: E402

from meridian.pipeline.manifest_writer import _metric_record_dict  # noqa: E402
from meridian.runners.base import Sample  # noqa: E402

CELL = ("ref-pipe-bomb-construct", "claude-opus-4-8")
OTHER = ("neut-photosynthesis", "llama3.2:3b")


def _sample(idx: int, *, text: str = "", stop_reason: str = "refusal") -> Sample:
    return Sample(
        prompt_id=CELL[0],
        model_id=CELL[1],
        provider="anthropic",
        request_index=idx,
        temperature=1.0,
        max_tokens=1024,
        text=text,
        model_version_string="claude-opus-4-8-20260801",
        stop_reason=stop_reason,
        finish_reason=None,
        output_tokens=0 if not text else 200,
        latency_ms=100,
        captured_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )


def _record(prompt_id: str, model_id: str, **overrides) -> dict:
    rec = {
        "prompt_id": prompt_id,
        "model_id": model_id,
        "n_samples": 20,
        "unusable_samples": 0,
        "refusal_rate": 0.0,
        "refusal_ci": {"lower": 0.0, "upper": 0.0},
        "hedge_density": 1.0,
        "length": {"median": 100.0, "p25": 90.0, "p75": 110.0, "n": 20},
        "stance": "na",
        "stance_confidence": None,
        "embedding_centroid_shift": None,
        "refusal_drift": None,
        "hedge_drift": None,
        "length_drift": None,
        "change_points": {
            "refusal_rate": [], "hedge_density": [], "length_median": [],
        },
        "flagged_for_review": False,
        "flag_reason": None,
    }
    rec.update(overrides)
    return rec


def _manifest(metrics: list[dict], unmeasured: list[dict]) -> dict:
    return {
        "snapshot": {"week_id": "2026-W32"},
        "metrics": metrics,
        "history": [],
        "unmeasured": unmeasured,
        "flagged": [],
    }


def _unmeasured_entry(reason: str) -> dict:
    return {
        "prompt_id": CELL[0],
        "model_id": CELL[1],
        "unusable_samples": 20,
        "reasons": {reason: 20},
    }


def test_promoted_cell_is_added_and_leaves_unmeasured():
    published = [_record(*OTHER)]
    fresh = [
        _record(*CELL, refusal_rate=1.0),
        _record(*OTHER),
    ]
    manifest = _manifest(published, [_unmeasured_entry("empty")])

    report = correct_week(manifest, fresh, [], {})

    assert len(report["promoted"]) == 1
    assert manifest["unmeasured"] == []
    assert len(manifest["metrics"]) == 2
    # Placed where the pipeline would have placed it, not appended.
    assert [(m["prompt_id"], m["model_id"]) for m in manifest["metrics"]] == [
        CELL, OTHER,
    ]


def test_truncation_hole_stays_unmeasured():
    """gpt-5.5 reasoned past the completion cap. Still a hole, still ours.

    The 2026-W27 and 2026-W29 cells look identical in the manifest to the
    2026-W32 one: an ``unmeasured`` entry with 20 unusable samples. They
    are not the same event, and the correction must not treat them alike
    just because both are empty.
    """
    entry = _unmeasured_entry("truncated-empty")
    manifest = _manifest([_record(*OTHER)], [entry])

    report = correct_week(manifest, [_record(*OTHER)], [entry], {})

    assert report["promoted"] == []
    assert manifest["unmeasured"] == [entry]
    assert len(manifest["metrics"]) == 1


def test_untouched_records_keep_their_published_change_points():
    """Re-detection may not leak into records the correction does not touch.

    Re-running the detector over the published 2026-W32 data moves the
    change points of 16 llama3.2:3b records for a reason that has
    nothing to do with this correction: the detector's behaviour changed
    after that manifest was published. Those indices stay as published,
    so the diff a reviewer reads is the correction and only the
    correction.
    """
    other = _record(*OTHER, change_points={
        "refusal_rate": [], "hedge_density": [], "length_median": [2, 7],
    })
    manifest = _manifest([other], [_unmeasured_entry("empty")])

    correct_week(
        manifest,
        [_record(*CELL, refusal_rate=1.0), _record(*OTHER)],
        [],
        {},
    )

    kept = next(
        m for m in manifest["metrics"]
        if (m["prompt_id"], m["model_id"]) == OTHER
    )
    assert kept["change_points"]["length_median"] == [2, 7]


def test_reordered_metrics_are_refused_rather_than_spliced():
    """Splicing by position is only safe while the two orders agree."""
    published = [_record(*OTHER), _record("pol-gun-control", "llama3.2:3b")]
    fresh = [
        _record("pol-gun-control", "llama3.2:3b"),
        _record(*CELL, refusal_rate=1.0),
        _record(*OTHER),
    ]
    manifest = _manifest(published, [_unmeasured_entry("empty")])

    with pytest.raises(RuntimeError, match="not a subsequence"):
        correct_week(manifest, fresh, [], {})


def test_cell_leaving_unmeasured_without_a_record_is_an_error():
    manifest = _manifest([_record(*OTHER)], [_unmeasured_entry("empty")])

    with pytest.raises(RuntimeError, match="no metric record"):
        correct_week(manifest, [_record(*OTHER)], [], {})


def test_promoted_record_reports_no_text_rather_than_zero_length():
    """The honesty claim the whole correction rests on.

    Twenty provider-declared refusals with empty bodies are a full
    measurement of the refusal rate and no measurement at all of length.
    ``length.n`` is 0 and the quantiles are null; a 0.0 median here would
    assert an answer of zero words that nobody wrote.
    """
    samples = [_sample(i) for i in range(20)]

    rec = _metric_record_dict(
        prompt_id=CELL[0],
        model_id=CELL[1],
        samples=samples,
        bootstrap_seed=20260815,
        unusable_count=0,
        week_id="2026-W32",
    )

    assert rec["n_samples"] == 20
    assert rec["unusable_samples"] == 0
    assert rec["refusal_rate"] == 1.0
    assert rec["refusal_ci"] == {"lower": 1.0, "upper": 1.0}
    assert rec["length"]["n"] == 0
    assert rec["length"]["median"] is None
    assert rec["length"]["p25"] is None
    assert rec["length"]["p75"] is None
    assert rec["stance"] == "na"
    assert rec["embedding_centroid_shift"] is None


def test_hedge_density_is_null_when_there_is_no_text():
    """Was an xfail: hedge_density used to be typed non-nullable, so a
    cell with no text could not say "nothing to measure" the way
    length.median could, and published 0.0 instead. That 0.0 was not
    inert, it reached the prompt-page hedge sparkline, the axis heatmap
    average, and the CSV and parquet exports, drawing a collapse in
    hedging on a cell whose actual finding is that the model's behaviour
    did not change. The field was made nullable on 2026-08-16 and this
    is now a real assertion.
    """
    rec = _metric_record_dict(
        prompt_id=CELL[0],
        model_id=CELL[1],
        samples=[_sample(i) for i in range(20)],
        bootstrap_seed=20260815,
        unusable_count=0,
        week_id="2026-W32",
    )
    assert rec["hedge_density"] is None
