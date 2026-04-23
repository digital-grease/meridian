"""ISO-week helpers."""
from __future__ import annotations

from datetime import date, datetime, timezone


def iso_week_for(d: date | datetime | None = None) -> str:
    """Format an ISO-week identifier like ``2026-W16`` for ``d``.

    Uses ISO 8601 week numbering (Monday-starting weeks; weeks belong to the
    year containing the Thursday). Defaults to today in UTC.
    """
    if d is None:
        d = datetime.now(timezone.utc).date()
    if isinstance(d, datetime):
        d = d.date()
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"
