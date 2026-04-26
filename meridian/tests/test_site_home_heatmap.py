"""Drift-heatmap helper tests for the home page.

Covers two scenarios:

  (a) one week of data (no history) — cells render in "absolute"
      mode and the rendered home page shows the snapshot caption.
  (b) two weeks (current + history) — cells render in "delta" mode
      and the per-cell metric matches the largest week-over-week
      shift on each (axis, model).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SITE_SRC = REPO_ROOT / "site" / "src"
if str(SITE_SRC) not in sys.path:
    sys.path.insert(0, str(SITE_SRC))

from build import drift_heatmap  # type: ignore[import-not-found]  # noqa: E402
from schema import Manifest  # type: ignore[import-not-found]  # noqa: E402


def _base_manifest_dict() -> dict:
    """Two prompts on two distinct axes, two models, no history."""
    return {
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
            {"model_id": "m2", "display_name": "M2",
             "provider": "fake", "version_string": "v1", "available": True},
        ],
        "prompts": [
            {"prompt_id": "p-pol", "axis": "political", "title": "pol",
             "text_hash": "a" * 64, "held_out": False},
            {"prompt_id": "p-sci", "axis": "scientific-consensus", "title": "sci",
             "text_hash": "b" * 64, "held_out": False},
        ],
        "metrics": [
            {"prompt_id": "p-pol", "model_id": "m1", "n_samples": 20,
             "refusal_rate": 0.10,
             "refusal_ci": {"lower": 0.0, "upper": 0.2}, "hedge_density": 1.0,
             "length": {"median": 100.0, "p25": 80.0, "p75": 120.0, "n": 20},
             "stance": "neutral", "stance_confidence": 0.8,
             "embedding_centroid_shift": None,
             "refusal_drift": None, "hedge_drift": None, "length_drift": None,
             "change_points": {"refusal_rate": [], "hedge_density": [], "length_median": []},
             "sample_s3_uris": [], "flagged_for_review": False, "flag_reason": None},
            {"prompt_id": "p-pol", "model_id": "m2", "n_samples": 20,
             "refusal_rate": 0.50,
             "refusal_ci": {"lower": 0.3, "upper": 0.7}, "hedge_density": 2.0,
             "length": {"median": 200.0, "p25": 180.0, "p75": 220.0, "n": 20},
             "stance": "neutral", "stance_confidence": 0.8,
             "embedding_centroid_shift": None,
             "refusal_drift": None, "hedge_drift": None, "length_drift": None,
             "change_points": {"refusal_rate": [], "hedge_density": [], "length_median": []},
             "sample_s3_uris": [], "flagged_for_review": False, "flag_reason": None},
            {"prompt_id": "p-sci", "model_id": "m1", "n_samples": 20,
             "refusal_rate": 0.05,
             "refusal_ci": {"lower": 0.0, "upper": 0.1}, "hedge_density": 0.5,
             "length": {"median": 150.0, "p25": 130.0, "p75": 170.0, "n": 20},
             "stance": "na", "stance_confidence": None,
             "embedding_centroid_shift": None,
             "refusal_drift": None, "hedge_drift": None, "length_drift": None,
             "change_points": {"refusal_rate": [], "hedge_density": [], "length_median": []},
             "sample_s3_uris": [], "flagged_for_review": False, "flag_reason": None},
            {"prompt_id": "p-sci", "model_id": "m2", "n_samples": 20,
             "refusal_rate": 0.05,
             "refusal_ci": {"lower": 0.0, "upper": 0.1}, "hedge_density": 0.4,
             "length": {"median": 145.0, "p25": 125.0, "p75": 165.0, "n": 20},
             "stance": "na", "stance_confidence": None,
             "embedding_centroid_shift": None,
             "refusal_drift": None, "hedge_drift": None, "length_drift": None,
             "change_points": {"refusal_rate": [], "hedge_density": [], "length_median": []},
             "sample_s3_uris": [], "flagged_for_review": False, "flag_reason": None},
        ],
        "history": [],
        "flagged": [],
        "silent_update_warnings": [],
    }


def test_heatmap_one_week_uses_absolute_mode():
    m = Manifest.model_validate(_base_manifest_dict())
    h = drift_heatmap(m)
    assert h["mode"] == "absolute"
    assert ("political", "m1") in h["cells"]
    assert ("political", "m2") in h["cells"]
    pol_m2 = h["cells"][("political", "m2")]
    assert pol_m2["mode"] == "absolute"
    # m2 has refusal_rate=0.5 → score = 0.5 / 1.0 = 0.5; m1 lower.
    assert pol_m2["score"] >= h["cells"][("political", "m1")]["score"]
    # max_score is the largest cell.
    assert h["max_score"] == max(c["score"] for c in h["cells"].values())


def test_heatmap_two_weeks_uses_delta_mode_with_dominant_metric():
    raw = _base_manifest_dict()
    raw["history"] = [{
        "week_id": "2026-W17",
        "generated_at": "2026-04-19T00:00:00+00:00",
        "metrics": [
            # Prior week: pol/m1 had refusal_rate 0.10 same. pol/m2 had
            # refusal_rate 0.10 (will jump to 0.50 → big delta).
            {"prompt_id": "p-pol", "model_id": "m1", "n_samples": 20,
             "refusal_rate": 0.10,
             "refusal_ci": {"lower": 0.0, "upper": 0.2}, "hedge_density": 1.0,
             "length": {"median": 100.0, "p25": 80.0, "p75": 120.0, "n": 20},
             "stance": "neutral", "stance_confidence": 0.8,
             "embedding_centroid_shift": None,
             "refusal_drift": None, "hedge_drift": None, "length_drift": None,
             "change_points": {"refusal_rate": [], "hedge_density": [], "length_median": []},
             "sample_s3_uris": [], "flagged_for_review": False, "flag_reason": None},
            {"prompt_id": "p-pol", "model_id": "m2", "n_samples": 20,
             "refusal_rate": 0.10,
             "refusal_ci": {"lower": 0.0, "upper": 0.2}, "hedge_density": 2.0,
             "length": {"median": 200.0, "p25": 180.0, "p75": 220.0, "n": 20},
             "stance": "neutral", "stance_confidence": 0.8,
             "embedding_centroid_shift": None,
             "refusal_drift": None, "hedge_drift": None, "length_drift": None,
             "change_points": {"refusal_rate": [], "hedge_density": [], "length_median": []},
             "sample_s3_uris": [], "flagged_for_review": False, "flag_reason": None},
            {"prompt_id": "p-sci", "model_id": "m1", "n_samples": 20,
             "refusal_rate": 0.05,
             "refusal_ci": {"lower": 0.0, "upper": 0.1}, "hedge_density": 0.5,
             "length": {"median": 150.0, "p25": 130.0, "p75": 170.0, "n": 20},
             "stance": "na", "stance_confidence": None,
             "embedding_centroid_shift": None,
             "refusal_drift": None, "hedge_drift": None, "length_drift": None,
             "change_points": {"refusal_rate": [], "hedge_density": [], "length_median": []},
             "sample_s3_uris": [], "flagged_for_review": False, "flag_reason": None},
            {"prompt_id": "p-sci", "model_id": "m2", "n_samples": 20,
             "refusal_rate": 0.05,
             "refusal_ci": {"lower": 0.0, "upper": 0.1}, "hedge_density": 0.4,
             "length": {"median": 145.0, "p25": 125.0, "p75": 165.0, "n": 20},
             "stance": "na", "stance_confidence": None,
             "embedding_centroid_shift": None,
             "refusal_drift": None, "hedge_drift": None, "length_drift": None,
             "change_points": {"refusal_rate": [], "hedge_density": [], "length_median": []},
             "sample_s3_uris": [], "flagged_for_review": False, "flag_reason": None},
        ],
    }]
    m = Manifest.model_validate(raw)
    h = drift_heatmap(m)
    assert h["mode"] == "delta"
    pol_m2 = h["cells"][("political", "m2")]
    assert pol_m2["mode"] == "delta"
    assert pol_m2["metric"] == "refusal_rate"
    # |0.50 - 0.10| / 1.0 = 0.40
    assert abs(pol_m2["score"] - 0.40) < 0.001
    # m2's pol cell should be the darkest in the grid.
    assert h["max_score"] == pol_m2["score"]


def test_heatmap_renders_in_home_page(tmp_path: Path):
    """End-to-end: builds the site from the synthetic fixture and
    verifies the heatmap section appears in index.html."""
    manifest = REPO_ROOT / "site" / "fixtures" / "synthetic-fixture.json"
    dist = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "run", "python", str(REPO_ROOT / "site" / "src" / "build.py"),
         "--manifest", str(manifest), "--out", str(dist)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    html = (dist / "index.html").read_text()
    assert 'class="drift-heatmap"' in html
    assert 'id="heatmap-heading"' in html
    assert 'class="heatmap-table"' in html


def test_heatmap_includes_carry_forward_with_history(tmp_path: Path):
    """Cadence-alternated models (Opus on even weeks, GPT-5.1 on odd)
    sit in the manifest as available=False but with their own data in
    the most recent history snapshot. The heatmap must include them
    so the grid doesn't lose half its frontier columns every week.

    Each carry-forward cell gets an ``as_of_week`` field pointing at
    the week the data is actually from, so the template can flag stale
    measurements visually.
    """
    raw = _base_manifest_dict()
    raw["models"].append({
        "model_id": "ghost", "display_name": "Ghost",
        "provider": "fake", "version_string": "v1", "available": False,
    })
    # Ghost has no current-week metrics — only history.
    raw["history"] = [{
        "week_id": "2026-W17",
        "generated_at": "2026-04-19T00:00:00+00:00",
        "metrics": [
            {"prompt_id": "p-pol", "model_id": "ghost", "n_samples": 20,
             "refusal_rate": 0.30,
             "refusal_ci": {"lower": 0.1, "upper": 0.5}, "hedge_density": 1.5,
             "length": {"median": 180.0, "p25": 160.0, "p75": 200.0, "n": 20},
             "stance": "neutral", "stance_confidence": 0.8,
             "embedding_centroid_shift": None,
             "refusal_drift": None, "hedge_drift": None, "length_drift": None,
             "change_points": {"refusal_rate": [], "hedge_density": [], "length_median": []},
             "sample_s3_uris": [], "flagged_for_review": False, "flag_reason": None},
        ],
    }]
    m = Manifest.model_validate(raw)
    h = drift_heatmap(m)
    assert "ghost" in h["models"], "carry-forward model should still get a heatmap column"
    ghost_cell = h["cells"][("political", "ghost")]
    assert ghost_cell["as_of_week"] == "2026-W17"
    assert ghost_cell["mode"] == "absolute"  # only one week of data
    assert h["has_stale"] is True


def test_heatmap_no_history_at_all_omits_carry_forward_models(tmp_path: Path):
    """If a carry-forward model has no history either (e.g. someone
    typoed the model_id during enrichment), the cell can't be filled
    and the column simply doesn't appear for that axis."""
    raw = _base_manifest_dict()
    raw["models"].append({
        "model_id": "phantom", "display_name": "Phantom",
        "provider": "fake", "version_string": "v1", "available": False,
    })
    m = Manifest.model_validate(raw)
    h = drift_heatmap(m)
    # Column appears in the model list (so the grid stays consistent),
    # but no cells render against it.
    assert "phantom" in h["models"]
    assert all(model_id != "phantom" for (_, model_id) in h["cells"])
