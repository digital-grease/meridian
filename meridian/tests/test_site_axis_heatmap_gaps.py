"""The per-axis model x week table must not render unsampled cells as 0.00.

Frontier models run on a biweekly cadence (Opus on even ISO weeks, GPT
on odd), so on any given week roughly half the roster has no samples at
all. The axis table previously averaged an empty list to a literal 0.0
and painted it with the same viridis ramp as a measured value, which
made "we did not run this model" indistinguishable from "this model
refused nothing", and made a steady 0.80-refusal model look like it was
sawtoothing between 0.80 and 0.00 every single week.

The distinction matters more here than anywhere else on the site: a
fabricated zero on the refusal axis is a false drift signal in the
exact measurement this project exists to publish.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_CHANGE_POINTS = {"refusal_rate": [], "hedge_density": [], "length_median": []}


def _metric(prompt_id: str, model_id: str, refusal_rate: float) -> dict:
    return {
        "prompt_id": prompt_id, "model_id": model_id, "n_samples": 20,
        "refusal_rate": refusal_rate,
        "refusal_ci": {"lower": refusal_rate, "upper": refusal_rate},
        "hedge_density": 1.0,
        "length": {"median": 100.0, "p25": 80.0, "p75": 120.0, "n": 20},
        "stance": "neutral", "stance_confidence": 0.8,
        "embedding_centroid_shift": None,
        "refusal_drift": None, "hedge_drift": None, "length_drift": None,
        "change_points": _CHANGE_POINTS,
        "flagged_for_review": False, "flag_reason": None,
    }


def _manifest() -> dict:
    """`cadence` runs only on W18 (refusing 0.80); `always` runs both
    weeks and genuinely refuses nothing.
    """
    return {
        "schema_version": 2,
        "snapshot": {
            "week_id": "2026-W18",
            "generated_at": "2026-04-25T00:00:00+00:00",
            "corpus_git_sha": "abc1234",
            "pipeline_version": "0.1.0",
        },
        "models": [
            {"model_id": "cadence", "display_name": "Cadence",
             "provider": "fake", "version_string": "v1", "available": True},
            {"model_id": "always", "display_name": "Always",
             "provider": "fake", "version_string": "v1", "available": True},
        ],
        "prompts": [
            {"prompt_id": "p-ref", "axis": "refusal-boundary", "title": "ref",
             "text_hash": "a" * 64, "held_out": False},
        ],
        # W18: both models sampled.
        "metrics": [
            _metric("p-ref", "cadence", 0.80),
            _metric("p-ref", "always", 0.0),
        ],
        # W17: `cadence` was off-cadence and produced no records at all.
        "history": [{
            "week_id": "2026-W17",
            "generated_at": "2026-04-19T00:00:00+00:00",
            "metrics": [_metric("p-ref", "always", 0.0)],
        }],
        "flagged": [],
        "silent_update_warnings": [],
    }


def _row_cells(html: str, display_name: str) -> list[str]:
    """Return the axis table's cells for one model row: a float string
    for a measured cell, or "EMPTY" for an unsampled one.
    """
    table = re.search(r'<table class="metric-table">.*?</table>', html, re.S)
    assert table, "axis page has no metric-table"
    for row in re.findall(r'<tr>\s*<th scope="row">.*?</tr>', table.group(0), re.S):
        if f">{display_name}</a>" not in row:
            continue
        cells = []
        for td in re.findall(r"<td\s.*?</td>", row, re.S):
            if "heatmap-empty" in td:
                cells.append("EMPTY")
            else:
                val = re.search(r"refusal rate </span>\s*([0-9.]+)", td)
                assert val, f"unparseable measured cell: {td}"
                cells.append(val.group(1))
        return cells
    raise AssertionError(f"no row for model {display_name!r}")


def test_axis_table_marks_unsampled_weeks_and_keeps_real_zeros(tmp_path: Path):
    manifest_path = tmp_path / "manifest-2026-W18.json"
    manifest_path.write_text(json.dumps(_manifest()))

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

    html = (dist / "axes" / "refusal-boundary" / "index.html").read_text()

    # The off-cadence week must be marked absent, NOT rendered 0.00.
    # The model refused 80% of prompts the one week it actually ran.
    assert _row_cells(html, "Cadence") == ["EMPTY", "0.80"]

    # A model that ran and genuinely refused nothing keeps its zeros:
    # the fix must not blank out real measurements of 0.
    assert _row_cells(html, "Always") == ["0.00", "0.00"]

    # The caption has to tell a reader what the marker means, since the
    # raw table is the citation surface for journalists. Collapse the
    # template's line wrapping before matching.
    caption = re.sub(r"\s+", " ", html)
    assert "was not sampled that week" in caption
    assert "is not a measurement of zero" in caption
