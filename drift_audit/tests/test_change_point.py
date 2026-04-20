"""Change-point detector tests against synthetic series with known transitions."""
from __future__ import annotations

import pytest

ruptures = pytest.importorskip("ruptures")

from drift_audit.analysis.change_point import detect_change_points  # noqa: E402


def _series(values: list[float], start_week: int = 13) -> list[tuple[str, float]]:
    return [(f"2026-W{start_week + i:02d}", v) for i, v in enumerate(values)]


def test_short_series_returns_empty():
    # Below MIN_SERIES_LEN — detector declines to hallucinate.
    assert detect_change_points(_series([0.1, 0.2])) == []


def test_no_change_no_flags():
    flat = [0.10, 0.11, 0.09, 0.10, 0.11, 0.12]
    assert detect_change_points(_series(flat)) == []


def test_single_step_change_detected_within_one_week():
    # Clean step from 0.10 to 0.70 at index 5.
    values = [0.10] * 5 + [0.70] * 5
    cps = detect_change_points(_series(values))
    assert len(cps) == 1
    assert abs(cps[0].index - 5) <= 1
    assert cps[0].magnitude > 0.4


def test_magnitudes_match_segment_means():
    values = [0.1] * 4 + [0.8] * 4
    cps = detect_change_points(_series(values))
    assert len(cps) == 1
    cp = cps[0]
    assert abs(cp.index - 4) <= 1
    assert cp.before_mean == pytest.approx(0.1, abs=0.1)
    assert cp.after_mean == pytest.approx(0.8, abs=0.1)


def test_two_change_points_on_three_regimes():
    # 0.1 -> 0.5 -> 0.9 staircase. Two equal-magnitude transitions;
    # penalty must be low enough that both splits beat a single-split
    # partition covering the middle regime in either direction.
    values = [0.1] * 4 + [0.5] * 4 + [0.9] * 4
    cps = detect_change_points(_series(values), penalty=0.1)
    indices = sorted(c.index for c in cps)
    assert any(3 <= i <= 5 for i in indices), f"no CP in first transition: {indices}"
    assert any(7 <= i <= 9 for i in indices), f"no CP in second transition: {indices}"


def test_penalty_controls_sensitivity():
    # Same series; high penalty suppresses change points.
    values = [0.1] * 4 + [0.2] * 4  # small step; borderline
    permissive = detect_change_points(_series(values), penalty=0.5)
    strict = detect_change_points(_series(values), penalty=10.0)
    assert len(strict) <= len(permissive)
