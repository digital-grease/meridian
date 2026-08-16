"""Change-point detection for per-(prompt, model, metric) weekly time series.

Answers "when did drift start?" rather than just "is today's number
different from last week's?" — a naive threshold fires multiple times
on a single real transition, which inflates false-positive counts and
obscures what actually happened.

Implementation uses PELT (Pruned Exact Linear Time) via the ``ruptures``
library. Its dependency footprint is moderate (numpy) and it's the
standard choice for 1-D change-point problems at this scale.

The detector is deliberately conservative on short series: with fewer
than ``MIN_SERIES_LEN`` points it returns no change points rather than
hallucinating them.

Gap awareness
-------------
The series is a list of ``(week_id, value)`` pairs, and the week ids are
load-bearing, not decoration. Until 2026-08 they were discarded before
the fit: the values were flattened into a bare array and PELT saw a
uniformly-spaced sequence. That is wrong whenever the model skipped a
scheduled week.

The 2026-W30 and 2026-W31 runs never happened (EC2
``InsufficientInstanceCapacity``, no data, permanently lost), so the
local-baseline series for ``llama3.2:3b`` jumps straight from 2026-W29
to 2026-W32. Positionally those two points are adjacent, so a 21-day
interval was being fitted exactly as if it were a 7-day one, and any
level shift accumulated across three weeks of outage would be reported
as a single week-over-week regime change. That series is the control
group whose noise floor is subtracted from every commercial drift
figure, so a false change point in it corrupts every published drift
number, not just its own row.

Two things follow, and both are implemented here:

1. The series is split into *cadence-contiguous runs* before fitting,
   and each run is fitted on its own. Segment means can therefore never
   mix values from opposite sides of an outage.
2. No change point is ever emitted at an interval wider than the
   expected cadence. When a metric moves across a three-week hole, the
   honest statement is "we do not know which of those weeks it moved
   in", not "it moved in 2026-W32".

``MIN_SERIES_LEN`` semantics are preserved and now apply per run: a run
with fewer than ``MIN_SERIES_LEN`` points yields no change points, the
same conservatism the whole-series check has always applied.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from meridian.sampling.weeks import iso_week_for

MIN_SERIES_LEN = 4

# Penalty controls change-point aggressiveness. Higher = fewer change
# points. For PELT with L2 cost on refusal-rate-scale data (typical
# noise std ~0.05), a penalty around 0.3–0.7 detects week-over-week
# shifts of ~0.15 or greater while ignoring single-week sampling noise.
# Callers should override for hedge density (different scale) or length.
DEFAULT_PENALTY = 0.5

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


@dataclass(frozen=True)
class ChangePoint:
    index: int          # position in the input series (0-based)
    week_id: str        # week at which the regime change begins
    before_mean: float  # mean of the segment preceding this change point
    after_mean: float   # mean of the segment starting at this change point
    magnitude: float    # absolute delta between segment means


def iso_week_start(week_id: str) -> date:
    """Return the Monday of ``week_id`` (e.g. ``2026-W32`` → 2026-08-03).

    Round-trips the parsed date back through
    :func:`meridian.sampling.weeks.iso_week_for` so the one ISO-week
    formatting rule in the codebase stays the only one, and so a week id
    that is syntactically well-formed but not a real ISO week (2026-W53,
    which does not exist) is rejected rather than silently normalised.

    Raises ``ValueError`` on anything that is not a ``YYYY-Www`` id.
    """
    m = _WEEK_RE.match(week_id)
    if not m:
        raise ValueError(f"not an ISO week id: {week_id!r}")
    year, week = int(m.group(1)), int(m.group(2))
    try:
        monday = date.fromisocalendar(year, week, 1)
    except ValueError as e:
        raise ValueError(f"not a real ISO week: {week_id!r}") from e
    if iso_week_for(monday) != week_id:  # pragma: no cover - defensive
        raise ValueError(f"week id does not round-trip: {week_id!r}")
    return monday


def weeks_between(earlier: str, later: str) -> int:
    """Whole ISO weeks from ``earlier`` to ``later``, e.g. W29 → W32 is 3.

    Negative if the arguments are the wrong way round, 0 if they are the
    same week. Correct across year boundaries (2025-W52 → 2026-W01 is 1)
    because it measures real calendar distance between week-start dates
    rather than subtracting week numbers.
    """
    return (iso_week_start(later) - iso_week_start(earlier)).days // 7


def expected_cadence_weeks(week_ids: list[str]) -> int:
    """Infer the routine spacing of a series, in ISO weeks.

    The roster does not run on one cadence. ``llama3.2:3b`` is sampled
    every week, while the frontier models alternate by ISO-week parity
    and so appear every second week. A single hard-coded "1 week"
    continuity rule would therefore suppress every change point for the
    commercial models the project exists to track, which is why the
    cadence is measured from the series instead of assumed.

    Uses the most common interval between consecutive weeks. On a tie
    the SMALLER interval wins, which is the conservative direction and
    the one the module principle above demands: over-stating the cadence
    swallows a real outage into a run and lets a change point be emitted
    across a hole, which is the single failure this module exists to
    prevent, while under-stating it only shortens runs until they fall
    below ``MIN_SERIES_LEN`` and the detector says nothing. Saying
    nothing is recoverable; "the local baseline shifted in 2026-W32" when
    the truth is "it shifted somewhere in W30 to W32" is published and
    wrong, and that series is the noise floor subtracted from every
    commercial drift figure.

    A caller that knows the real schedule should pass ``cadence_weeks``
    to :func:`detect_change_points` rather than rely on inference.

    Returns 1 for a series with fewer than two points, which has no
    observable cadence and cannot produce a change point anyway.
    """
    if len(week_ids) < 2:
        return 1
    gaps = [
        gap
        for gap in (weeks_between(a, b) for a, b in pairwise(week_ids))
        if gap > 0
    ]
    if not gaps:
        return 1
    counts = Counter(gaps)
    top = max(counts.values())
    return min(g for g, c in counts.items() if c == top)


def _contiguous_runs(
    week_ids: list[str], cadence: int
) -> list[tuple[int, int]]:
    """Split positions into ``(start, end)`` half-open runs of on-cadence weeks.

    A run break is any interval wider than ``cadence``, which is to say
    any week the model was scheduled for and did not produce, whether
    that is an infrastructure outage (2026-W30 and 2026-W31) or a
    provider being unreachable.
    """
    runs: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(week_ids)):
        if weeks_between(week_ids[i - 1], week_ids[i]) > cadence:
            runs.append((start, i))
            start = i
    runs.append((start, len(week_ids)))
    return runs


def detect_change_points(
    series: list[tuple[str, float]],
    *,
    penalty: float = DEFAULT_PENALTY,
    cadence_weeks: int | None = None,
) -> list[ChangePoint]:
    """Run PELT on a (week_id, value) series. Series must be sorted oldest-first.

    Returns change points where a regime change begins. ``index`` is a
    position in the *input* series (so the site can point at the right
    cell of the rendered sparkline), and the metric from
    ``series[cp.index]`` onward belongs to the new segment.

    Week ids are carried through to the fit. The series is split into
    runs of consecutive on-cadence weeks and each run is fitted
    separately, so no change point is ever emitted across a wider gap
    and no segment mean ever averages across one. Pass
    ``cadence_weeks`` to state the expected spacing explicitly,
    otherwise it is inferred with :func:`expected_cadence_weeks`.

    Raises ``ValueError`` if any week id is not a ``YYYY-Www`` ISO week
    id: without parseable weeks there is no way to tell a real
    week-over-week transition from a shift accumulated across an
    outage, and guessing is what this function exists to stop.
    """
    if len(series) < MIN_SERIES_LEN:
        return []

    try:
        import numpy as np
        import ruptures as rpt
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "change_point.detect_change_points requires the `changepoint` dep "
            "group. Install with: uv sync --group changepoint"
        ) from e

    week_ids = [w for w, _ in series]
    cadence = (
        cadence_weeks if cadence_weeks is not None
        else expected_cadence_weeks(week_ids)
    )

    out: list[ChangePoint] = []
    for run_start, run_end in _contiguous_runs(week_ids, cadence):
        # Same conservatism as the whole-series guard above, applied per
        # run: too few on-cadence points to say anything, so say nothing.
        #
        # At the current MIN_SERIES_LEN=4 this only changes the outcome
        # for a one-point run, where `rpt.Pelt(min_size=2)` raises
        # BadSegmentationParameters rather than returning nothing. Runs
        # of two or three points are already unsplittable under
        # min_size=2, so for those the guard is redundant, not
        # load-bearing. It is kept because the MIN_SERIES_LEN contract is
        # stated per run in this module's docstring and because the two
        # numbers move independently: raise MIN_SERIES_LEN and this is
        # the only thing enforcing it.
        if run_end - run_start < MIN_SERIES_LEN:
            continue
        values = np.array(
            [v for _, v in series[run_start:run_end]], dtype=float
        ).reshape(-1, 1)
        # jump=1 lets PELT consider every position as a candidate breakpoint.
        # min_size=2 prevents singleton segments. At 52 weeks/year scale this
        # is cheap; defaults (jump=5) are meant for very long sequences.
        algo = rpt.Pelt(model="l2", jump=1, min_size=2).fit(values)
        # ruptures returns breakpoints as segment END indices (1-indexed style):
        # [5, 12] on a 12-point series means segment 1 = values[:5],
        # segment 2 = values[5:12]. We treat each non-terminal breakpoint as
        # the START of a new regime and compute segment-local means.
        breakpoints = algo.predict(pen=penalty)
        boundaries = [0] + breakpoints  # [0, b1, b2, ..., len(values)]

        for i in range(1, len(boundaries) - 1):
            bp = boundaries[i]
            before_start = boundaries[i - 1]
            after_end = boundaries[i + 1]
            before = values[before_start:bp].mean()
            after = values[bp:after_end].mean()
            out.append(
                ChangePoint(
                    # Offset back into the caller's series so indices stay
                    # comparable across runs.
                    index=run_start + bp,
                    week_id=series[run_start + bp][0],
                    before_mean=round(float(before), 4),
                    after_mean=round(float(after), 4),
                    magnitude=round(abs(float(after) - float(before)), 4),
                )
            )
    return out
