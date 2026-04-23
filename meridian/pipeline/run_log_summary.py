"""Weekly rollup over the append-only pipeline run log.

`run_log.jsonl` is authoritative but too granular for a health dashboard;
this module folds multiple invocations per week into one row.

Used by the internal `/internal/health/` page (site builder reads via
`load_run_log_summary`). Not used by the public dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from meridian.pipeline.run_log import RunLogEntry


@dataclass(frozen=True)
class WeeklySummary:
    week_id: str
    latest_finished_at: str
    total_samples_written: int
    pairs_complete: int
    pairs_skipped: int
    pairs_failed: int
    estimated_cost_usd: float
    actual_cost_usd: float
    runner_count: int
    error_count: int
    cost_overrun_pct: float | None  # None when estimate is zero

    @property
    def pairs_total(self) -> int:
        return self.pairs_complete + self.pairs_skipped + self.pairs_failed


def summarize_weekly(entries: Iterable[RunLogEntry]) -> list[WeeklySummary]:
    """Fold RunLogEntries into one WeeklySummary per week_id.

    A run can be retried within the same week; the latest finished_at
    per week wins. Pairs counts, samples, and costs take the latest
    entry's values rather than summing retries — the last entry is the
    authoritative state at week close.

    Ordering: newest week first (reverse chronological by week_id).
    """
    by_week: dict[str, RunLogEntry] = {}
    for e in entries:
        prev = by_week.get(e.week_id)
        if prev is None or e.finished_at > prev.finished_at:
            by_week[e.week_id] = e

    summaries: list[WeeklySummary] = []
    for e in by_week.values():
        overrun: float | None
        if e.estimated_cost_usd > 0:
            overrun = (e.actual_cost_usd - e.estimated_cost_usd) / e.estimated_cost_usd * 100.0
        else:
            overrun = None
        summaries.append(
            WeeklySummary(
                week_id=e.week_id,
                latest_finished_at=e.finished_at,
                total_samples_written=e.total_samples_written,
                pairs_complete=e.pairs_complete,
                pairs_skipped=e.pairs_skipped,
                pairs_failed=e.pairs_failed,
                estimated_cost_usd=e.estimated_cost_usd,
                actual_cost_usd=e.actual_cost_usd,
                runner_count=len(e.runners),
                error_count=len(e.errors),
                cost_overrun_pct=overrun,
            )
        )

    summaries.sort(key=lambda s: s.week_id, reverse=True)
    return summaries
