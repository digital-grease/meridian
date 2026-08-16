"""Change-point detector tests against synthetic series with known transitions."""
from __future__ import annotations

import pytest

ruptures = pytest.importorskip("ruptures")

from meridian.analysis.change_point import (  # noqa: E402
    detect_change_points,
    expected_cadence_weeks,
    iso_week_start,
    weeks_between,
)


def _series(values: list[float], start_week: int = 13) -> list[tuple[str, float]]:
    return [(f"2026-W{start_week + i:02d}", v) for i, v in enumerate(values)]


def _weeks(week_numbers: list[int], values: list[float]) -> list[tuple[str, float]]:
    """Pair explicit ISO week numbers with values, for irregular cadences."""
    assert len(week_numbers) == len(values)
    return [
        (f"2026-W{n:02d}", v)
        for n, v in zip(week_numbers, values, strict=True)
    ]


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


# --- ISO-week arithmetic -------------------------------------------------


def test_weeks_between_counts_real_calendar_distance():
    assert weeks_between("2026-W29", "2026-W32") == 3   # the W30/W31 outage
    assert weeks_between("2026-W15", "2026-W16") == 1
    assert weeks_between("2026-W16", "2026-W16") == 0
    assert weeks_between("2026-W16", "2026-W14") == -2


def test_weeks_between_crosses_year_boundary():
    # Week numbers alone would say 52 - 1 = -51. Only real dates get this
    # right, and prior-week resolution genuinely walks across new year.
    assert weeks_between("2025-W52", "2026-W01") == 1
    assert weeks_between("2026-W52", "2026-W53") == 1  # 2026 is a 53-week year


def test_iso_week_start_rejects_malformed_and_unreal_weeks():
    assert iso_week_start("2026-W32").isoformat() == "2026-08-03"  # a Monday
    for bad in ("2026W32", "26-W32", "2026-W3", "<current>", "", "2027-W53"):
        with pytest.raises(ValueError):
            iso_week_start(bad)


def test_expected_cadence_infers_weekly_and_biweekly():
    # Local baseline: every week, with the W30/W31 hole. The hole is the
    # exception, so the cadence is still 1.
    weekly = [f"2026-W{n:02d}" for n in (26, 27, 28, 29, 32, 33)]
    assert expected_cadence_weeks(weekly) == 1
    # Frontier roster: even ISO weeks only, so a two-week cadence is normal
    # and must NOT be treated as a gap.
    biweekly = [f"2026-W{n:02d}" for n in (16, 18, 20, 22, 24)]
    assert expected_cadence_weeks(biweekly) == 2
    assert expected_cadence_weeks(["2026-W16"]) == 1


def test_tied_cadence_resolves_to_the_smaller_interval():
    """A tie must not be resolved by widening the cadence.

    Gaps of 1, 1, 3, 3 give no majority. Taking the larger interval
    declares three weeks routine, folds a real outage into a run, and
    lets a change point be emitted across a hole, which is the single
    failure this module exists to prevent. Taking the smaller only
    shortens runs until they fall below MIN_SERIES_LEN and the detector
    says nothing, which is recoverable.
    """
    tied = [f"2026-W{n:02d}" for n in (20, 21, 22, 25, 28)]
    assert expected_cadence_weeks(tied) == 1


def test_tie_break_suppresses_a_change_point_across_the_hole():
    """The tie-break, observed through the detector rather than asserted
    on the helper. The step lands exactly on the three-week interval.
    """
    series = _weeks(
        [20, 21, 22, 25, 28],
        [0.10, 0.10, 0.10, 0.80, 0.80],
    )
    assert detect_change_points(series) == []
    # Told explicitly that three weeks is the real cadence, the same data
    # is one contiguous run and the step is reported. That is what proves
    # the suppression above comes from the tie-break and not from the
    # values being unremarkable.
    forced = detect_change_points(series, cadence_weeks=3)
    assert [c.index for c in forced] == [3]
    assert forced[0].week_id == "2026-W25"


# --- gap awareness -------------------------------------------------------


def test_no_change_point_across_a_missing_run():
    """The 2026-W30/W31 outage regression.

    Weeks 30 and 31 produced no data at all (EC2 capacity), so W29 and
    W32 are three weeks apart. Discarding the week ids made them look
    adjacent and turned a shift accumulated over three weeks into a
    week-over-week regime change on the local-baseline control series,
    whose noise floor is subtracted from every published drift figure.
    """
    series = _weeks(
        [26, 27, 28, 29, 32, 33, 34, 35],
        [0.10] * 4 + [0.70] * 4,
    )
    assert detect_change_points(series) == []
    # Same values with no hole in the calendar: the step is real and is
    # reported, which is what proves the suppression above is about the
    # gap and not about the data.
    contiguous = _series([0.10] * 4 + [0.70] * 4)
    cps = detect_change_points(contiguous)
    assert [c.index for c in cps] == [4]
    assert cps[0].week_id == "2026-W17"


def test_gap_does_not_suppress_change_points_inside_a_run():
    """A hole later in the series must not blind the detector to a real
    transition that happened entirely before it. Runs are fitted
    independently, so the pre-outage weeks keep their own change point.
    """
    series = _weeks(
        [20, 21, 22, 23, 24, 25, 32, 33],
        [0.10, 0.10, 0.10, 0.80, 0.80, 0.80, 0.80, 0.80],
    )
    indices = [c.index for c in detect_change_points(series)]
    assert indices == [3], indices


def test_segment_means_never_average_across_a_gap():
    """Post-outage values must not leak into a pre-outage segment mean.

    The run ending at W29 is fitted on its own six points, so the
    reported ``after_mean`` is the pre-gap regime, not a blend of it with
    whatever came back in W32.
    """
    series = _weeks(
        [24, 25, 26, 27, 28, 29, 32, 33, 34, 35],
        [0.10, 0.10, 0.10, 0.90, 0.90, 0.90, 0.50, 0.50, 0.50, 0.50],
    )
    cps = detect_change_points(series)
    pre_gap = [c for c in cps if c.index <= 5]
    assert pre_gap, f"expected the pre-outage transition to survive: {cps}"
    # 0.9, the pre-gap regime, not (0.9 * 3 + 0.5 * 4) / 7 = 0.671.
    assert pre_gap[0].after_mean == pytest.approx(0.9, abs=0.01)
    # And nothing is ever reported as beginning at the first post-gap week.
    assert all(c.week_id != "2026-W32" for c in cps)


def test_biweekly_cadence_is_not_treated_as_a_gap():
    """Frontier models alternate by ISO-week parity, so consecutive
    samples are two weeks apart by design. A hard-coded one-week
    continuity rule would silence change-point detection for exactly the
    commercial models the project exists to track.
    """
    series = _weeks(
        [16, 18, 20, 22, 24, 26],
        [0.10, 0.10, 0.10, 0.75, 0.75, 0.75],
    )
    indices = [c.index for c in detect_change_points(series)]
    assert indices == [3], indices


def test_gap_wider_than_biweekly_cadence_still_splits():
    # Even-week model that then misses four scheduled weeks: W22 -> W30 is
    # 8 weeks, far outside its two-week cadence.
    series = _weeks(
        [16, 18, 20, 22, 30, 32, 34, 36],
        [0.10, 0.10, 0.10, 0.10, 0.80, 0.80, 0.80, 0.80],
    )
    assert detect_change_points(series) == []


def test_explicit_cadence_overrides_inference():
    # Inference would call this weekly (three 1-week gaps against one
    # 3-week gap) and split at W32. Told the cadence is 3, the whole
    # series is one run and the step is reported.
    series = _weeks(
        [26, 27, 28, 29, 32, 33, 34, 35],
        [0.10] * 4 + [0.70] * 4,
    )
    assert [c.index for c in detect_change_points(series, cadence_weeks=3)] == [4]
    assert detect_change_points(series, cadence_weeks=1) == []


def test_run_shorter_than_min_series_len_yields_nothing():
    # Post-gap run of three points is below MIN_SERIES_LEN, so it gets the
    # same treatment a three-point series has always got: no change points.
    #
    # Honest note on what this does and does not pin: at three points the
    # per-run MIN_SERIES_LEN guard is redundant, because rpt.Pelt is
    # constructed with min_size=2 and cannot place an interior breakpoint
    # in a three-point run either way. This fixture would return [] with
    # the guard removed. The length where the guard actually changes the
    # outcome is one point, covered by the test below.
    series = _weeks(
        [20, 21, 22, 23, 30, 31, 32],
        [0.10, 0.10, 0.10, 0.10, 0.90, 0.10, 0.90],
    )
    assert detect_change_points(series) == []


def test_single_point_run_after_a_gap_is_skipped_not_fitted():
    """The per-run MIN_SERIES_LEN guard at the length where it bites.

    Splitting on cadence can leave a run of one week, and
    ``rpt.Pelt(min_size=2)`` raises ``BadSegmentationParameters`` when
    handed a one-point array rather than returning no breakpoints. The
    guard is what turns that into silence.

    The pre-gap run is fitted normally in the same call, so a lone
    trailing week cannot take the rest of the series down with it.
    """
    series = _weeks(
        [20, 21, 22, 23, 24, 25, 40],
        [0.10, 0.10, 0.10, 0.90, 0.90, 0.90, 0.90],
    )
    cps = detect_change_points(series)
    assert [c.index for c in cps] == [3], cps
    assert cps[0].week_id == "2026-W23"


def test_unparseable_week_id_raises():
    """Without parseable weeks there is no way to distinguish a real
    week-over-week transition from a shift accumulated across an outage,
    so the detector refuses rather than guessing.
    """
    series = [("<current>", 0.1), ("<current>", 0.1), ("x", 0.9), ("y", 0.9)]
    with pytest.raises(ValueError):
        detect_change_points(series)
