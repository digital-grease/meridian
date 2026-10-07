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

    A run can be retried or resumed within the same week, and the two
    kinds of number in an entry fold differently:

    * Samples are summed. Each invocation counts only the samples it
      wrote itself, so a resumed run that skipped every stored pair
      records 0 here. Taking the latest entry alone reported a week whose
      first run wrote 750 samples and whose resume wrote 0 as a week of
      0 samples, and a week whose resume finished the job as a week of
      only the resume's share.
    * Pair counts, errors and costs come from the latest entry. A retry
      re-attempts the pairs that failed and skips the ones already stored,
      so its pair counts are the state of the week at close, and
      ``actual_cost_usd`` is already summed over every sample stored for
      the week.

    Ordering: newest week first (reverse chronological by week_id).
    """
    by_week: dict[str, RunLogEntry] = {}
    samples_by_week: dict[str, int] = {}
    for e in entries:
        samples_by_week[e.week_id] = (
            samples_by_week.get(e.week_id, 0) + e.total_samples_written
        )
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
                total_samples_written=samples_by_week[e.week_id],
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
