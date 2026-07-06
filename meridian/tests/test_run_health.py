"""Tests for scripts/check_run_health.py — the publish-time guard that
turns the weekly workflow red when a run recorded failed pairs/errors."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_run_health.py"
_spec = importlib.util.spec_from_file_location("check_run_health", _SCRIPT)
crh = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(crh)


def _entry(week, *, complete, failed, errors=()):
    return {
        "week_id": week,
        "pairs_complete": complete,
        "pairs_failed": failed,
        "pairs_skipped": 0,
        "errors": list(errors),
    }


def test_latest_entry_picks_most_recent_for_week():
    entries = [
        _entry("2026-W26", complete=60, failed=0),
        _entry("2026-W27", complete=30, failed=30),   # failed run
        _entry("2026-W27", complete=60, failed=0),     # later re-run, clean
    ]
    got = crh.latest_entry_for_week(entries, "2026-W27")
    assert got is not None and got["pairs_failed"] == 0  # last wins


def test_missing_week_returns_none():
    assert crh.latest_entry_for_week([_entry("2026-W26", complete=60, failed=0)], "2026-W99") is None


def test_clean_run_is_healthy():
    healthy, _ = crh.evaluate(_entry("2026-W26", complete=60, failed=0))
    assert healthy is True


def test_failed_pairs_are_unhealthy():
    # Mirrors the real 2026-W27 gpt-5.5 temperature=0 400 storm.
    err = {
        "provider": "openai",
        "model_id": "gpt-5.5",
        "error_type": "UpstreamError",
        "message": "Error code: 400 - 'temperature' does not support 0 ...",
    }
    healthy, detail = crh.evaluate(_entry("2026-W27", complete=30, failed=30, errors=[err]))
    assert healthy is False
    assert "gpt-5.5" in detail and "30 failed pair" in detail


def test_errors_without_failed_count_still_unhealthy():
    healthy, _ = crh.evaluate(
        _entry("2026-W27", complete=60, failed=0, errors=[{"message": "x"}])
    )
    assert healthy is False


def test_main_exit_codes(tmp_path):
    log = tmp_path / "run_log.jsonl"
    import json
    log.write_text(
        json.dumps(_entry("2026-W26", complete=60, failed=0)) + "\n"
        + json.dumps(_entry("2026-W27", complete=30, failed=30, errors=[{"message": "x"}])) + "\n",
        encoding="utf-8",
    )
    assert crh.main(["2026-W26", "--run-log", str(log)]) == 0
    assert crh.main(["2026-W27", "--run-log", str(log)]) == 1
    assert crh.main(["2026-W99", "--run-log", str(log)]) == 1  # missing week


def test_main_missing_run_log(tmp_path):
    assert crh.main(["2026-W27", "--run-log", str(tmp_path / "nope.jsonl")]) == 1
