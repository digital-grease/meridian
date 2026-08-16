"""A drift p-value has to say what it was compared against.

``DriftTest`` carries a p-value and a BH-adjusted p-value and, until
2026-08-15, nothing about the baseline. The only reading a reader can
give an unlabelled drift number is "this changed since last week", and
that reading is usually wrong: the commercial roster alternates by
ISO-week parity, so a frontier model's nearest prior run is normally two
calendar weeks back, and after the 2026-W30 / 2026-W31 outage it was
four.

``compared_to_week`` and ``weeks_elapsed`` carry the baseline. Both are
optional, because every manifest published from 2026-W17 through
2026-W32 predates them and the site must keep rendering all of them.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "site" / "src"))

from build import drift_baselines  # noqa: E402
from schema import DriftTest, Manifest  # noqa: E402

_CHANGE_POINTS = {"refusal_rate": [], "hedge_density": [], "length_median": []}


def _metric(model_id: str, drift: dict | None) -> dict:
    return {
        "prompt_id": "p-one", "model_id": model_id, "n_samples": 20,
        "refusal_rate": 0.1,
        "refusal_ci": {"lower": 0.0, "upper": 0.2},
        "hedge_density": 1.0,
        "length": {"median": 100.0, "p25": 80.0, "p75": 120.0, "n": 20},
        "stance": "neutral", "stance_confidence": 0.8,
        "embedding_centroid_shift": None,
        "refusal_drift": drift, "hedge_drift": None, "length_drift": None,
        "change_points": _CHANGE_POINTS,
        "flagged_for_review": False, "flag_reason": None,
    }


def _drift(week: str | None, elapsed: int | None) -> dict:
    d = {"p_value": 0.03, "adjusted_p_value": 0.09, "significant_after_bh": False}
    if week is not None:
        d["compared_to_week"] = week
    if elapsed is not None:
        d["weeks_elapsed"] = elapsed
    return d


def _manifest_dict(metrics: list[dict]) -> dict:
    return {
        "schema_version": 2,
        "snapshot": {
            "week_id": "2026-W32",
            "generated_at": "2026-08-10T00:00:00+00:00",
            "corpus_git_sha": "abc1234",
            "pipeline_version": "0.1.0",
        },
        "models": [
            {"model_id": "opus", "display_name": "Opus", "provider": "fake",
             "version_string": "v1", "available": True},
            {"model_id": "gpt", "display_name": "GPT", "provider": "fake",
             "version_string": "v1", "available": True},
        ],
        "prompts": [
            {"prompt_id": "p-one", "axis": "neutral-control", "title": "One",
             "text_hash": "a" * 64, "held_out": False},
        ],
        "metrics": metrics,
        "history": [],
        "flagged": [],
        "silent_update_warnings": [],
    }


# ---------------------------------------------------------------------
# Schema compatibility
# ---------------------------------------------------------------------


def test_drift_test_validates_without_the_new_fields():
    """Every manifest from 2026-W17 to 2026-W32 lacks them. If this
    breaks, the archive stops rendering."""
    d = DriftTest.model_validate(
        {"p_value": 0.5, "adjusted_p_value": 1.0, "significant_after_bh": False}
    )
    assert d.compared_to_week is None
    assert d.weeks_elapsed is None


def test_drift_test_accepts_the_new_fields():
    d = DriftTest.model_validate(_drift("2026-W28", 4))
    assert d.compared_to_week == "2026-W28"
    assert d.weeks_elapsed == 4


def test_weeks_elapsed_below_one_is_rejected():
    """A test cannot compare a week against itself, and the interval is
    never negative. Catching it here is loud; letting it through would
    publish a nonsense interval next to a p-value."""
    with pytest.raises(ValidationError):
        DriftTest.model_validate(_drift("2026-W32", 0))


def test_a_published_manifest_still_validates():
    """Guards the whole archive, not just a hand-built record."""
    newest = sorted((REPO_ROOT / "site" / "fixtures").glob("manifest-*.json"))[-1]
    Manifest.model_validate(json.loads(newest.read_text()))


# ---------------------------------------------------------------------
# Baseline roll-up
# ---------------------------------------------------------------------


def test_drift_baselines_groups_by_week_and_interval():
    m = Manifest.model_validate(_manifest_dict([
        _metric("opus", _drift("2026-W28", 4)),
        _metric("gpt", _drift("2026-W29", 3)),
    ]))
    rows = drift_baselines(m)
    assert rows == [
        {"week_id": "2026-W29", "weeks_elapsed": 3, "tests": 1},
        {"week_id": "2026-W28", "weeks_elapsed": 4, "tests": 1},
    ]


def test_drift_baselines_is_empty_for_a_manifest_without_the_fields():
    m = Manifest.model_validate(_manifest_dict([
        _metric("opus", _drift(None, None)),
    ]))
    assert drift_baselines(m) == []


def test_drift_baselines_is_empty_when_no_drift_tests_ran():
    m = Manifest.model_validate(_manifest_dict([_metric("opus", None)]))
    assert drift_baselines(m) == []


# ---------------------------------------------------------------------
# The reader sees it
# ---------------------------------------------------------------------


def _build(tmp_path: Path, manifest: dict) -> Path:
    manifest_path = tmp_path / "manifest-2026-W32.json"
    manifest_path.write_text(json.dumps(manifest))
    dist = tmp_path / "dist"
    result = subprocess.run(
        [
            "uv", "run", "python",
            str(REPO_ROOT / "site" / "src" / "build.py"),
            "--manifest", str(manifest_path),
            "--out", str(dist),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"site build failed: {result.stderr}"
    return dist


def test_methodology_names_the_comparison_week(tmp_path: Path):
    dist = _build(tmp_path, _manifest_dict([
        _metric("opus", _drift("2026-W28", 4)),
        _metric("gpt", _drift("2026-W29", 3)),
    ]))
    page = re.sub(r"\s+", " ", (dist / "methodology" / "index.html").read_text())

    # The general rule, so a reader knows the baseline is not always
    # the previous calendar week.
    assert "What a drift p-value is compared against." in page
    assert "compared_to_week" in page

    # And this snapshot's actual baselines, with the interval spelled out.
    assert "2026-W28" in page
    assert "4 weeks earlier" in page
    assert "3 weeks earlier" in page

    # The "what's measured" row names them too, so the baseline is
    # visible without reading the prose.
    assert "compared against" in page


def test_methodology_falls_back_when_the_manifest_predates_the_fields(tmp_path: Path):
    dist = _build(tmp_path, _manifest_dict([_metric("opus", _drift(None, None))]))
    page = re.sub(r"\s+", " ", (dist / "methodology" / "index.html").read_text())
    assert "What a drift p-value is compared against." in page
    assert "does not name the baseline for each drift result" in page
