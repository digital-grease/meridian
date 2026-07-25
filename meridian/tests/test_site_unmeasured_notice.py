"""A prompt page must disclose cells we sampled but could not measure.

The site's 2026-W28 gap fix taught the axis table to distinguish "not
sampled" from "measured zero", keyed on an absent MetricRecord. That
cannot catch the case where the record is absent because every response
came back empty: to the template both look identical. The manifest's
``unmeasured`` list carries the difference and the page has to say so,
or a reader sees a model silently missing from a contested prompt with
no explanation.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_CHANGE_POINTS = {"refusal_rate": [], "hedge_density": [], "length_median": []}


def _metric(prompt_id: str, model_id: str) -> dict:
    return {
        "prompt_id": prompt_id, "model_id": model_id, "n_samples": 20,
        "unusable_samples": 0,
        "refusal_rate": 0.0, "refusal_ci": {"lower": 0.0, "upper": 0.0},
        "hedge_density": 1.0,
        "length": {"median": 100.0, "p25": 80.0, "p75": 120.0, "n": 20},
        "stance": "neutral", "stance_confidence": 0.8,
        "embedding_centroid_shift": None,
        "refusal_drift": None, "hedge_drift": None, "length_drift": None,
        "change_points": _CHANGE_POINTS,
        "flagged_for_review": False, "flag_reason": None,
    }


def _manifest(*, with_unmeasured: bool) -> dict:
    return {
        "schema_version": 2,
        "snapshot": {
            "week_id": "2026-W30",
            "generated_at": "2026-07-27T00:00:00+00:00",
            "corpus_git_sha": "abc1234",
            "pipeline_version": "0.1.0",
        },
        "models": [
            {"model_id": "reasoner", "display_name": "Reasoner",
             "provider": "fake", "version_string": "v1", "available": True},
            {"model_id": "control", "display_name": "Control",
             "provider": "fake", "version_string": "v1", "available": True},
        ],
        "prompts": [
            {"prompt_id": "sci-contested", "axis": "scientific-consensus",
             "title": "Contested science", "text_hash": "a" * 64,
             "held_out": False},
        ],
        # `reasoner` produced no measurable record for the prompt.
        "metrics": [_metric("sci-contested", "control")],
        "history": [],
        "unmeasured": (
            [{
                "prompt_id": "sci-contested", "model_id": "reasoner",
                "unusable_samples": 20,
                "reasons": {"truncated-empty": 20},
            }] if with_unmeasured else []
        ),
        "flagged": [],
        "silent_update_warnings": [],
    }


def _build(tmp_path: Path, manifest: dict) -> Path:
    manifest_path = tmp_path / "manifest-2026-W30.json"
    manifest_path.write_text(json.dumps(manifest))
    dist = tmp_path / "dist"
    result = subprocess.run(
        [
            "uv", "run", "python", str(REPO_ROOT / "site" / "src" / "build.py"),
            "--manifest", str(manifest_path), "--out", str(dist),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"site build failed: {result.stderr}"
    return dist


def test_prompt_page_discloses_unmeasured_cell(tmp_path: Path):
    dist = _build(tmp_path, _manifest(with_unmeasured=True))
    html = (dist / "prompts" / "sci-contested" / "index.html").read_text()
    flat = re.sub(r"\s+", " ", html)

    assert "Not measurable this week" in flat
    assert "reasoner" in flat
    assert "truncated-empty" in flat
    assert "20" in flat
    # It must be framed as our gap, not the model's behaviour: this is
    # the whole reason the section exists.
    assert "not a finding about the model" in flat


def test_no_notice_when_everything_measured(tmp_path: Path):
    dist = _build(tmp_path, _manifest(with_unmeasured=False))
    html = (dist / "prompts" / "sci-contested" / "index.html").read_text()
    assert "Not measurable this week" not in html
