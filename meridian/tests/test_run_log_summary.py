"""Unit tests for meridian.pipeline.run_log_summary.summarize_weekly."""
from __future__ import annotations

from meridian.pipeline.run_log import RunLogEntry
from meridian.pipeline.run_log_summary import WeeklySummary, summarize_weekly


def _entry(
    *,
    week_id: str,
    finished_at: str,
    samples: int = 100,
    complete: int = 10,
    skipped: int = 0,
    failed: int = 0,
    estimated: float = 5.0,
    actual: float = 5.0,
    runners: int = 2,
    errors: int = 0,
) -> RunLogEntry:
    return RunLogEntry(
        started_at="2026-04-19T00:00:00+00:00",
        finished_at=finished_at,
        week_id=week_id,
        host="h",
        pid=1,
        config_hash="abc1234567890def",
        runners=[f"r{i}/m{i}" for i in range(runners)],
        total_samples_written=samples,
        pairs_complete=complete,
        pairs_skipped=skipped,
        pairs_failed=failed,
        per_runner_samples={},
        estimated_cost_usd=estimated,
        actual_cost_usd=actual,
        errors=[{"provider": "x"} for _ in range(errors)],
        note=None,
    )


def test_summarize_empty():
    assert summarize_weekly([]) == []


def test_summarize_single_week_single_entry():
    e = _entry(week_id="2026-W16", finished_at="2026-04-19T12:00:00+00:00")
    [s] = summarize_weekly([e])
    assert s.week_id == "2026-W16"
    assert s.pairs_total == 10
    assert s.cost_overrun_pct == 0.0


def test_summarize_two_weeks_sorted_newest_first():
    a = _entry(week_id="2026-W15", finished_at="2026-04-12T12:00:00+00:00")
    b = _entry(week_id="2026-W16", finished_at="2026-04-19T12:00:00+00:00")
    out = summarize_weekly([a, b])
    assert [s.week_id for s in out] == ["2026-W16", "2026-W15"]


def test_summarize_retry_within_same_week_picks_latest():
    # Two entries for W16 — second run retried after first; counts improved.
    first = _entry(
        week_id="2026-W16", finished_at="2026-04-19T12:00:00+00:00",
        complete=5, failed=5, actual=3.0,
    )
    retry = _entry(
        week_id="2026-W16", finished_at="2026-04-19T18:00:00+00:00",
        complete=10, failed=0, actual=5.0,
    )
    [s] = summarize_weekly([first, retry])
    assert s.pairs_complete == 10
    assert s.pairs_failed == 0
    assert s.actual_cost_usd == 5.0


def test_summarize_cost_overrun_pct():
    e = _entry(
        week_id="2026-W16", finished_at="2026-04-19T00:00:00+00:00",
        estimated=4.0, actual=5.0,
    )
    [s] = summarize_weekly([e])
    assert s.cost_overrun_pct is not None
    assert abs(s.cost_overrun_pct - 25.0) < 1e-9


def test_summarize_cost_overrun_undefined_when_estimate_zero():
    e = _entry(
        week_id="2026-W16", finished_at="2026-04-19T00:00:00+00:00",
        estimated=0.0, actual=1.2,
    )
    [s] = summarize_weekly([e])
    assert s.cost_overrun_pct is None


def test_summarize_error_and_runner_counts():
    e = _entry(
        week_id="2026-W16", finished_at="2026-04-19T00:00:00+00:00",
        runners=3, errors=4,
    )
    [s] = summarize_weekly([e])
    assert s.runner_count == 3
    assert s.error_count == 4


def test_weekly_summary_pairs_total():
    s = WeeklySummary(
        week_id="w", latest_finished_at="t",
        total_samples_written=0,
        pairs_complete=3, pairs_skipped=1, pairs_failed=2,
        estimated_cost_usd=0.0, actual_cost_usd=0.0,
        runner_count=0, error_count=0, cost_overrun_pct=None,
    )
    assert s.pairs_total == 6
