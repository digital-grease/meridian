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
"""
from __future__ import annotations

from dataclasses import dataclass

MIN_SERIES_LEN = 4

# Penalty controls change-point aggressiveness. Higher = fewer change
# points. For PELT with L2 cost on refusal-rate-scale data (typical
# noise std ~0.05), a penalty around 0.3–0.7 detects week-over-week
# shifts of ~0.15 or greater while ignoring single-week sampling noise.
# Callers should override for hedge density (different scale) or length.
DEFAULT_PENALTY = 0.5


@dataclass(frozen=True)
class ChangePoint:
    index: int          # position in the input series (0-based)
    week_id: str        # week at which the regime change begins
    before_mean: float  # mean of the segment preceding this change point
    after_mean: float   # mean of the segment starting at this change point
    magnitude: float    # absolute delta between segment means


def detect_change_points(
    series: list[tuple[str, float]],
    *,
    penalty: float = DEFAULT_PENALTY,
) -> list[ChangePoint]:
    """Run PELT on a (week_id, value) series. Series must be sorted oldest-first.

    Returns change points where a regime change begins. The first element of
    each returned tuple is the *start* of a new regime — the metric from
    week[cp.index] onward belongs to the new segment.
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

    values = np.array([v for _, v in series], dtype=float).reshape(-1, 1)
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

    out: list[ChangePoint] = []
    for i in range(1, len(boundaries) - 1):
        bp = boundaries[i]
        before_start = boundaries[i - 1]
        after_end = boundaries[i + 1]
        before = values[before_start:bp].mean()
        after = values[bp:after_end].mean()
        out.append(
            ChangePoint(
                index=bp,
                week_id=series[bp][0],
                before_mean=round(float(before), 4),
                after_mean=round(float(after), 4),
                magnitude=round(abs(float(after) - float(before)), 4),
            )
        )
    return out
