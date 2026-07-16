"""Per-axis headline-metric selection for the axis model x week table.

refusal_rate is only meaningful on refusal-boundary; on the other axes
every measured cell is 0.00, so the table has to lead with the metric
that actually moves there (hedge density for stance-bearing axes,
length for the silent-update control axes).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import get_args

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SITE_SRC = REPO_ROOT / "site" / "src"
if str(SITE_SRC) not in sys.path:
    sys.path.insert(0, str(SITE_SRC))

import pytest  # noqa: E402
from build import (  # type: ignore[import-not-found]  # noqa: E402
    _AXIS_HEADLINE_METRIC,
    _METRIC_META,
    axis_metric_table,
)
from schema import Axis, Manifest  # type: ignore[import-not-found]  # noqa: E402

_CHANGE_POINTS = {"refusal_rate": [], "hedge_density": [], "length_median": []}


def _metric(prompt_id, model_id, *, refusal=0.0, hedge=1.0, length=100.0):
    return {
        "prompt_id": prompt_id, "model_id": model_id, "n_samples": 20,
        "refusal_rate": refusal,
        "refusal_ci": {"lower": refusal, "upper": refusal},
        "hedge_density": hedge,
        "length": {"median": length, "p25": length, "p75": length, "n": 20},
        "stance": "neutral", "stance_confidence": 0.8,
        "embedding_centroid_shift": None,
        "refusal_drift": None, "hedge_drift": None, "length_drift": None,
        "change_points": _CHANGE_POINTS,
        "sample_s3_uris": [], "flagged_for_review": False, "flag_reason": None,
    }


def _manifest(axis: str, *, metrics, history_metrics=None) -> Manifest:
    raw = {
        "schema_version": 2,
        "snapshot": {
            "week_id": "2026-W18",
            "generated_at": "2026-04-25T00:00:00+00:00",
            "corpus_git_sha": "abc1234",
            "pipeline_version": "0.1.0",
        },
        "models": [
            {"model_id": "m1", "display_name": "M1",
             "provider": "fake", "version_string": "v1", "available": True},
        ],
        "prompts": [
            {"prompt_id": "p1", "axis": axis, "title": "p1",
             "text_hash": "a" * 64, "held_out": False},
        ],
        "metrics": metrics,
        "history": [],
        "flagged": [],
        "silent_update_warnings": [],
    }
    if history_metrics is not None:
        raw["history"] = [{
            "week_id": "2026-W17",
            "generated_at": "2026-04-19T00:00:00+00:00",
            "metrics": history_metrics,
        }]
    return Manifest.model_validate(raw)


@pytest.mark.parametrize(
    ("axis", "expected"),
    [
        ("refusal-boundary", "refusal_rate"),
        ("political", "hedge_density"),
        ("historical-contested", "hedge_density"),
        ("scientific-consensus", "hedge_density"),
        ("neutral-control", "length_median"),
        ("factual-stability", "length_median"),
    ],
)
def test_each_axis_leads_with_the_metric_that_moves(axis, expected):
    m = _manifest(axis, metrics=[_metric("p1", "m1")])
    assert axis_metric_table(m, axis)["metric"] == expected


def test_every_schema_axis_has_a_deliberate_metric():
    """Axis is a closed Literal in the schema, so the mapping can be
    exhaustive. Adding an axis to the corpus should fail here and force
    a deliberate choice, rather than silently defaulting to
    refusal_rate, the metric that is flat on most axes and caused the
    wall-of-zeros in the first place.
    """
    schema_axes = set(get_args(Axis))
    assert set(_AXIS_HEADLINE_METRIC) == schema_axes
    assert set(_AXIS_HEADLINE_METRIC.values()) <= set(_METRIC_META)


def test_refusal_rate_ramp_is_pinned_to_one():
    """A probability reads the same on every page, so its ramp must not
    rescale to whatever the busiest cell happens to be."""
    m = _manifest("refusal-boundary", metrics=[_metric("p1", "m1", refusal=0.2)])
    assert axis_metric_table(m, "refusal-boundary")["max"] == 1.0


def test_open_ended_metric_scales_to_the_data():
    m = _manifest("neutral-control", metrics=[_metric("p1", "m1", length=250.0)])
    assert axis_metric_table(m, "neutral-control")["max"] == 250.0


def test_all_zero_table_does_not_divide_by_zero():
    """An axis where every cell is 0 must still render, at the ramp
    floor, rather than blowing up on a zero-width colour domain."""
    m = _manifest("neutral-control", metrics=[_metric("p1", "m1", length=0.0)])
    t = axis_metric_table(m, "neutral-control")
    assert t["max"] == 1.0
    assert t["rows"][0]["cells"] == [0.0]


def test_unsampled_week_is_none_not_zero():
    """The core invariant: absent data is None, never a float."""
    m = _manifest(
        "refusal-boundary",
        metrics=[_metric("p1", "m1", refusal=0.8)],
        history_metrics=[],           # m1 not sampled in W17
    )
    t = axis_metric_table(m, "refusal-boundary")
    assert t["weeks"] == ["2026-W17", "2026-W18"]
    assert t["rows"][0]["cells"] == [None, 0.8]
    assert t["has_gaps"] is True


def test_has_gaps_false_when_every_week_sampled():
    """Tables with no gaps must not print the gap-marker legend."""
    m = _manifest(
        "refusal-boundary",
        metrics=[_metric("p1", "m1", refusal=0.8)],
        history_metrics=[_metric("p1", "m1", refusal=0.4)],
    )
    t = axis_metric_table(m, "refusal-boundary")
    assert t["rows"][0]["cells"] == [0.4, 0.8]
    assert t["has_gaps"] is False
