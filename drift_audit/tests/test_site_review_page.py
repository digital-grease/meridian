"""Review queue page tests.

Two scenarios cover the branches of site/src/templates/review.html:
  (a) empty manifest — page exists, empty-state paragraphs, no tables.
  (b) populated manifest — silent-update warning + flagged metric both
      render with the expected severity / n_samples.
"""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _run_build(tmp_path: Path, manifest_path: Path) -> Path:
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
    assert result.returncode == 0, (
        f"build failed: {result.stderr}\n{result.stdout}"
    )
    return dist


def _base_manifest() -> dict:
    """Minimal but complete manifest: one model, one prompt, one metric.

    Fixture shape copied from drift_audit/tests/test_cli_inspect.py to
    keep one source of truth within this test suite.
    """
    return {
        "schema_version": 2,
        "snapshot": {
            "week_id": "2026-W16",
            "generated_at": "2026-04-19T00:00:00+00:00",
            "corpus_git_sha": "abc1234",
            "pipeline_version": "0.1.0",
        },
        "models": [{
            "model_id": "fake-model-1",
            "display_name": "Fake Model",
            "provider": "fake",
            "version_string": "fake-2026-04-19",
            "available": True,
        }],
        "prompts": [{
            "prompt_id": "p1", "axis": "neutral-control", "title": "t",
            "text_hash": "0" * 64, "held_out": False,
        }],
        "metrics": [{
            "prompt_id": "p1",
            "model_id": "fake-model-1",
            "n_samples": 20,
            "refusal_rate": 0.05,
            "refusal_ci": {"lower": 0.0, "upper": 0.1},
            "hedge_density": 1.2,
            "length": {"median": 100.0, "p25": 80.0, "p75": 120.0, "n": 20},
            "stance": "neutral",
            "stance_confidence": 0.85,
            "embedding_centroid_shift": None,
            "refusal_drift": None,
            "hedge_drift": None,
            "length_drift": None,
            "change_points": {
                "refusal_rate": [], "hedge_density": [], "length_median": [],
            },
            "sample_s3_uris": [],
            "flagged_for_review": False,
            "flag_reason": None,
        }],
        "history": [],
        "flagged": [],
        "silent_update_warnings": [],
    }


def test_review_page_empty_state(tmp_path: Path):
    manifest = _base_manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    dist = _run_build(tmp_path, manifest_path)
    review_html = (dist / "review" / "index.html").read_text()

    assert 'name="robots"' in review_html and "noindex" in review_html
    assert "No candidates this week." in review_html
    assert "No insufficient-data flags this week." in review_html
    # Empty-state must not emit a populated <tbody>.
    assert "severity-high" not in review_html
    assert "severity-medium" not in review_html
    assert "severity-low" not in review_html


def test_review_page_populated(tmp_path: Path):
    manifest = _base_manifest()
    # One silent-update warning on the neutral-control axis.
    manifest["silent_update_warnings"] = [{
        "model_id": "fake-model-1",
        "from_week": "2026-W15",
        "to_week": "2026-W16",
        "axis": "neutral-control",
        "metric": "length_median",
        "from_value": 100.0,
        "to_value": 140.0,
        "delta": 40.0,
        "severity": "high",
    }]
    # One metric flagged for insufficient data.
    flagged_metric = copy.deepcopy(manifest["metrics"][0])
    flagged_metric["n_samples"] = 3
    flagged_metric["flagged_for_review"] = True
    flagged_metric["flag_reason"] = "n_samples=3 < MIN_SAMPLES_FOR_PUBLICATION=10"
    manifest["metrics"].append(flagged_metric)
    # Distinct prompt_id to keep the two metrics distinguishable.
    manifest["metrics"][0]["prompt_id"] = "p-ok"
    manifest["prompts"].append({
        "prompt_id": "p-ok", "axis": "neutral-control", "title": "ok",
        "text_hash": "1" * 64, "held_out": False,
    })

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    dist = _run_build(tmp_path, manifest_path)
    review_html = (dist / "review" / "index.html").read_text()

    # Silent-update row rendered with severity badge + transition + delta.
    assert "severity-high" in review_html
    assert "2026-W15" in review_html
    assert "2026-W16" in review_html
    assert "+40.000" in review_html
    assert "length median" in review_html  # metric name underscore-stripped

    # Insufficient-data row rendered with n_samples + reason.
    assert "<code>3</code>" in review_html
    assert "MIN_SAMPLES_FOR_PUBLICATION" in review_html

    # Empty-state copy should NOT appear now that both tables have rows.
    assert "No candidates this week." not in review_html
    assert "No insufficient-data flags this week." not in review_html
