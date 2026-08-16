"""A measured record carrying no response text must render, not crash.

Since 2026-08-15 a sample can be measured and still have an empty body:
a provider refusal arrives as ``stop_reason='refusal'`` with no message,
so ``refusal_rate`` is a real 1.00 while every text-derived metric is
genuinely absent. ``LengthStats.median`` was made nullable to express
that, and the pipeline emits None rather than a fabricated 0.

The templates were not all updated with it. ``model_week.html`` rendered
``{{ "%.0f" % m.length.median }}`` unguarded, so the first such record
aborted the whole build with ``TypeError: must be real number, not
NoneType`` and emitted zero pages. Because ``build.py`` renders a
model-week page for every week in ``manifest.history``, one such record
would have kept crashing every build for the following eight weeks, i.e.
meridianaudit.org would have stopped updating entirely.

It had not fired only because no published manifest contained such a
record yet. The next one was due 2026-08-24, the next claude-opus-4-8
week, on ``ref-pipe-bomb-construct``.

These tests pin the whole class: a record that was sampled, is measured,
and has no text must render on every page type that shows a text-derived
metric, and must never present the absence as a zero.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_CHANGE_POINTS = {"refusal_rate": [], "hedge_density": [], "length_median": []}


def _metric(*, textless: bool) -> dict:
    """One metric record. ``textless`` is the API-refusal shape."""
    return {
        "prompt_id": "ref-boundary", "model_id": "reasoner", "n_samples": 20,
        "unusable_samples": 0,
        # Refusal is measured either way. That is the point: the cell is
        # not a hole, it is a cell whose text-derived metrics are absent.
        "refusal_rate": 1.0, "refusal_ci": {"lower": 1.0, "upper": 1.0},
        "hedge_density": None if textless else 0.5,
        "length": (
            {"median": None, "p25": None, "p75": None, "n": 0}
            if textless
            else {"median": 100.0, "p25": 80.0, "p75": 120.0, "n": 20}
        ),
        "stance": "na", "stance_confidence": None,
        "embedding_centroid_shift": None,
        "refusal_drift": None, "hedge_drift": None, "length_drift": None,
        "change_points": _CHANGE_POINTS,
        "flagged_for_review": False, "flag_reason": None,
    }


def _manifest(*, textless: bool) -> dict:
    return {
        "schema_version": 2,
        "snapshot": {
            "week_id": "2026-W32",
            "generated_at": "2026-08-10T00:00:00+00:00",
            "corpus_git_sha": "abc1234",
            "pipeline_version": "0.1.0",
        },
        "models": [
            {"model_id": "reasoner", "display_name": "Reasoner",
             "provider": "fake", "version_string": "v1", "available": True},
        ],
        "prompts": [
            {"prompt_id": "ref-boundary", "axis": "refusal-boundary",
             "title": "Refusal boundary", "text_hash": "a" * 64,
             "held_out": False},
        ],
        "metrics": [_metric(textless=textless)],
        "history": [],
        "unmeasured": [],
        "flagged": [],
        "silent_update_warnings": [],
    }


def _build(tmp_path: Path, manifest: dict) -> Path:
    manifest_path = tmp_path / "manifest-2026-W32.json"
    manifest_path.write_text(json.dumps(manifest))
    dist = tmp_path / "dist"
    result = subprocess.run(
        [
            "uv", "run", "python", str(REPO_ROOT / "site" / "src" / "build.py"),
            "--manifest", str(manifest_path), "--out", str(dist),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        "site build failed on a measured record with no response text; "
        f"this is the crash class model_week.html:44 shipped with.\n"
        f"{result.stderr}\n{result.stdout}"
    )
    return dist


def test_build_survives_a_measured_record_with_no_text(tmp_path: Path):
    dist = _build(tmp_path, _manifest(textless=True))
    # The build must actually produce the page, not merely exit 0.
    page = dist / "models" / "reasoner" / "2026-W32" / "index.html"
    assert page.exists(), "model-week page was not emitted"


def test_absent_text_metrics_do_not_render_as_zero(tmp_path: Path):
    """The absence must not be dressed up as a measurement.

    A median of 0 on this row would assert the model answered with zero
    words, which is the fabricated-zero failure the usability module
    exists to prevent, one layer up in the template.
    """
    dist = _build(tmp_path, _manifest(textless=True))
    html = (dist / "models" / "reasoner" / "2026-W32" / "index.html").read_text()
    flat = re.sub(r"\s+", " ", html)

    assert "no response text to measure" in flat
    # The refusal measurement itself is still published.
    assert "1.00" in flat
    # No zero-valued text metric anywhere in the row.
    assert "<code>0</code>" not in flat
    assert "<code>0.00</code>" not in flat


def test_a_normal_record_still_renders_its_numbers(tmp_path: Path):
    """Guard against the fix suppressing real measurements."""
    dist = _build(tmp_path, _manifest(textless=False))
    html = (dist / "models" / "reasoner" / "2026-W32" / "index.html").read_text()
    flat = re.sub(r"\s+", " ", html)

    assert "<code>100</code>" in flat, "median length went missing"
    assert "<code>0.50</code>" in flat, "hedge density went missing"
    assert "no response text to measure" not in flat
