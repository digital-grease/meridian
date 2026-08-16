"""Internal pipeline-health page tests.

The page reads a run log via load_run_log_summary. Two scenarios cover
the empty-log branch (default state) and the populated branch (two
scripted entries produce two rows).

These tests used to swap the real ``data/run_log.jsonl`` in place and
restore it in a finally block. On 2026-08-15 two pytest processes ran
concurrently, the second captured the first's scripted file as "the
original", and restoring it destroyed 15 of the 17 real entries. They
came back from git, but the run log is the append-only public record
and retention is forever, so the pattern is gone: the scripted log now
lives in tmp_path and reaches the build through MERIDIAN_RUN_LOG. No
test writes to data/ at all.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from meridian.pipeline.run_log import RunLogEntry

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _run_build(tmp_path: Path, run_log: Path) -> Path:
    """Build the site with ``run_log`` standing in for the real log.

    ``run_log`` may point at a path that does not exist; that is how the
    empty-state case is expressed, and read_run_log returns [] for it.
    """
    dist = tmp_path / "dist"
    manifest = REPO_ROOT / "site" / "fixtures" / "synthetic-fixture.json"
    result = subprocess.run(
        [
            "uv", "run", "python",
            str(REPO_ROOT / "site" / "src" / "build.py"),
            "--manifest", str(manifest),
            "--out", str(dist),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
        env={**os.environ, "MERIDIAN_RUN_LOG": str(run_log)},
    )
    assert result.returncode == 0, (
        f"build failed: {result.stderr}\n{result.stdout}"
    )
    return dist


def _write_run_log(path: Path, entries: list[RunLogEntry]) -> Path:
    lines = [json.dumps(asdict(e), sort_keys=True) for e in entries]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return path


def _entry(
    *,
    week_id: str,
    finished: str,
    complete: int,
    failed: int,
    actual: float,
) -> RunLogEntry:
    return RunLogEntry(
        started_at=finished,  # not asserted
        finished_at=finished,
        week_id=week_id,
        host="ci",
        pid=1,
        config_hash="deadbeefdeadbeef",
        runners=["fake/model-a"],
        total_samples_written=complete * 20,
        pairs_complete=complete,
        pairs_skipped=0,
        pairs_failed=failed,
        per_runner_samples={"fake/model-a": complete * 20},
        estimated_cost_usd=1.0,
        actual_cost_usd=actual,
        errors=[],
        note=None,
    )


def test_health_page_empty_state(tmp_path: Path):
    # A path that does not exist is the empty-log case.
    dist = _run_build(tmp_path, tmp_path / "absent-run-log.jsonl")

    html = (dist / "internal" / "health" / "index.html").read_text()
    assert 'name="robots"' in html and "noindex" in html
    assert "No run log captured yet." in html
    # No table row tokens should be present.
    assert "Weekly rollup" not in html or "<tbody>" not in html


def test_health_page_with_two_weeks(tmp_path: Path):
    entries = [
        _entry(
            week_id="2026-W15",
            finished="2026-04-12T12:00:00+00:00",
            complete=8, failed=2, actual=1.50,
        ),
        _entry(
            week_id="2026-W16",
            finished="2026-04-19T12:00:00+00:00",
            complete=10, failed=0, actual=1.25,
        ),
    ]
    run_log = _write_run_log(tmp_path / "run_log.jsonl", entries)
    dist = _run_build(tmp_path, run_log)

    html = (dist / "internal" / "health" / "index.html").read_text()
    # Both weeks appear; newest first.
    w15 = html.find("2026-W15")
    w16 = html.find("2026-W16")
    assert w15 > 0 and w16 > 0, "expected both weeks in rendered HTML"
    assert w16 < w15, "expected newest week (W16) before W15"

    # Expect cost-overrun annotation for W16 (estimate=1.0, actual=1.25 → +25%).
    assert "+25.0%" in html
    # Sample count for W16 (10 pairs × 20 samples).
    assert "<code>200</code>" in html

    # Empty-state copy should not appear when rows exist.
    assert "No run log captured yet." not in html
