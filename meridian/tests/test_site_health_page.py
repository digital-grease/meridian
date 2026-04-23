"""Internal pipeline-health page tests.

The page reads data/run_log.jsonl via load_run_log_summary. Two
scenarios cover the empty-log branch (default state) and the
populated branch (two scripted entries produce two rows).

Uses the try/finally seed-and-restore pattern that test_redirects.py
established, so the test is safe when a real run log already exists
on disk.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from meridian.pipeline.run_log import RunLogEntry

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _run_build(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    manifest = REPO_ROOT / "site" / "fixtures" / "manifest-2026-W16.json"
    result = subprocess.run(
        [
            "uv", "run", "python",
            str(REPO_ROOT / "site" / "src" / "build.py"),
            "--manifest", str(manifest),
            "--out", str(dist),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"build failed: {result.stderr}\n{result.stdout}"
    )
    return dist


def _swap_run_log(entries: list[RunLogEntry] | None) -> bytes | None:
    """Swap data/run_log.jsonl with a scripted version and return a
    token (original bytes, or None if the file did not exist) that the
    caller passes to _restore_run_log in a finally block."""
    log_path = REPO_ROOT / "data" / "run_log.jsonl"
    original: bytes | None
    original = log_path.read_bytes() if log_path.exists() else None

    log_path.parent.mkdir(parents=True, exist_ok=True)
    if entries is None:
        if log_path.exists():
            log_path.unlink()
    else:
        lines = [json.dumps(asdict(e), sort_keys=True) for e in entries]
        log_path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return original


def _restore_run_log(token: bytes | None) -> None:
    log_path = REPO_ROOT / "data" / "run_log.jsonl"
    if token is None:
        if log_path.exists():
            log_path.unlink()
    else:
        log_path.write_bytes(token)


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
    token = _swap_run_log(None)
    try:
        dist = _run_build(tmp_path)
    finally:
        _restore_run_log(token)

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
    token = _swap_run_log(entries)
    try:
        dist = _run_build(tmp_path)
    finally:
        _restore_run_log(token)

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
