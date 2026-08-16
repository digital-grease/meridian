"""Weeks in which the audit ran nothing must be shown, not closed up.

2026-W30 and 2026-W31 produced no samples for any model: the weekly run
never started, because the orchestrator's EC2 request came back
``InsufficientInstanceCapacity`` two Mondays running. The data is gone
for good.

Before this, the site had no representation for such a week. It was
absent from ``all_weeks`` so no table had a column for it, absent from
``timeseries`` so every sparkline drew 2026-W29 joined straight onto
2026-W32 by one unbroken line, and absent from ``/data/`` so the
snapshot index listed the two weeks adjacent with nothing to say
otherwise. Meanwhile /methodology/ told readers that missing weeks
render "as a break in the line". A published claim that does not hold
is worse than no claim.

These tests pin the three surfaces: the calendar-true week list, the
broken line, and the visible gap row in the data index.

A week where *some* models ran is deliberately not covered by any of
this. That is the biweekly frontier cadence working as designed, and
conflating it with a lost week would reduce every alternating model's
chart to a row of disconnected dots.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "site" / "src"))

from chart import sparkline  # noqa: E402
from schema import MISSING_WEEK, Manifest, is_measured, week_span  # noqa: E402

_CHANGE_POINTS = {"refusal_rate": [], "hedge_density": [], "length_median": []}


def _metric(prompt_id: str, model_id: str, refusal_rate: float, **kw) -> dict:
    rec = {
        "prompt_id": prompt_id, "model_id": model_id, "n_samples": 20,
        "refusal_rate": refusal_rate,
        "refusal_ci": {"lower": refusal_rate, "upper": refusal_rate},
        "hedge_density": 1.0,
        "length": {"median": 100.0, "p25": 80.0, "p75": 120.0, "n": 20},
        "stance": "neutral", "stance_confidence": 0.8,
        "embedding_centroid_shift": None,
        "refusal_drift": None, "hedge_drift": None, "length_drift": None,
        "change_points": _CHANGE_POINTS,
        "flagged_for_review": False, "flag_reason": None,
    }
    rec.update(kw)
    return rec


def _manifest_dict(
    *,
    snapshot_week: str = "2026-W32",
    history_weeks: tuple[str, ...] = ("2026-W28", "2026-W29"),
    weekly_model_only_in: tuple[str, ...] | None = None,
    change_points: dict | None = None,
) -> dict:
    """A manifest whose history skips 2026-W30 and 2026-W31 entirely.

    ``weekly`` runs every week that exists; ``biweekly`` runs only in the
    weeks named by ``weekly_model_only_in``, standing in for the
    ISO-week-parity roster.
    """
    biweekly_weeks = weekly_model_only_in or ("2026-W28", "2026-W32")
    cp = change_points or _CHANGE_POINTS

    def _week_metrics(week: str) -> list[dict]:
        out = [_metric("p-one", "weekly", 0.10, change_points=cp)]
        if week in biweekly_weeks:
            out.append(_metric("p-one", "biweekly", 0.20, change_points=cp))
        return out

    return {
        "schema_version": 2,
        "snapshot": {
            "week_id": snapshot_week,
            "generated_at": "2026-08-10T00:00:00+00:00",
            "corpus_git_sha": "abc1234",
            "pipeline_version": "0.1.0",
        },
        "models": [
            {"model_id": "weekly", "display_name": "Weekly",
             "provider": "fake", "version_string": "v1", "available": True},
            {"model_id": "biweekly", "display_name": "Biweekly",
             "provider": "fake", "version_string": "v1", "available": True},
        ],
        "prompts": [
            {"prompt_id": "p-one", "axis": "neutral-control", "title": "One",
             "text_hash": "a" * 64, "held_out": False},
        ],
        "metrics": _week_metrics(snapshot_week),
        "history": [
            {"week_id": w, "generated_at": "2026-07-01T00:00:00+00:00",
             "metrics": _week_metrics(w)}
            for w in history_weeks
        ],
        "flagged": [],
        "silent_update_warnings": [],
    }


def _manifest(**kw) -> Manifest:
    return Manifest.model_validate(_manifest_dict(**kw))


# ---------------------------------------------------------------------
# Week arithmetic
# ---------------------------------------------------------------------


def test_week_span_is_inclusive_and_calendar_stepped():
    assert week_span("2026-W28", "2026-W32") == [
        "2026-W28", "2026-W29", "2026-W30", "2026-W31", "2026-W32",
    ]


def test_week_span_crosses_a_year_boundary():
    span = week_span("2026-W52", "2027-W02")
    assert span[0] == "2026-W52"
    assert span[-1] == "2027-W02"
    assert "2027-W01" in span


def test_week_span_degrades_to_empty_on_a_label_it_cannot_parse():
    """A malformed week label must not take the public record offline."""
    assert week_span("garbage", "2026-W32") == []
    assert week_span("2026-W32", "2026-W28") == []


# ---------------------------------------------------------------------
# Manifest week lists
# ---------------------------------------------------------------------


def test_missing_weeks_finds_the_outage():
    m = _manifest()
    assert m.observed_weeks == ["2026-W28", "2026-W29", "2026-W32"]
    assert m.missing_weeks == ["2026-W30", "2026-W31"]


def test_all_weeks_keeps_a_column_for_every_calendar_week():
    assert _manifest().all_weeks == [
        "2026-W28", "2026-W29", "2026-W30", "2026-W31", "2026-W32",
    ]


def test_contiguous_history_reports_no_missing_weeks():
    m = _manifest(
        snapshot_week="2026-W29", history_weeks=("2026-W27", "2026-W28"),
    )
    assert m.missing_weeks == []
    assert m.all_weeks == m.observed_weeks


def test_single_week_manifest_has_no_window_to_search():
    m = _manifest(snapshot_week="2026-W32", history_weeks=())
    assert m.missing_weeks == []
    assert m.all_weeks == ["2026-W32"]


# ---------------------------------------------------------------------
# timeseries gap sentinels
# ---------------------------------------------------------------------


def test_timeseries_carries_a_gap_sentinel_for_a_lost_week():
    series = _manifest().timeseries("p-one", "weekly", "refusal_rate")
    assert [w for w, _v in series] == [
        "2026-W28", "2026-W29", "2026-W30", "2026-W31", "2026-W32",
    ]
    assert series[2][1] is MISSING_WEEK
    assert series[3][1] is MISSING_WEEK
    assert [is_measured(v) for _w, v in series] == [
        True, True, False, False, True,
    ]


def test_cadence_skipped_week_is_not_a_gap_sentinel():
    """`biweekly` did not run in 2026-W29, but the audit did. That is a
    roster decision, not a hole in the record, so the series simply has
    no 2026-W29 point rather than a sentinel."""
    series = _manifest().timeseries("p-one", "biweekly", "refusal_rate")
    weeks = [w for w, _v in series]
    assert "2026-W29" not in weeks
    assert weeks == ["2026-W28", "2026-W30", "2026-W31", "2026-W32"]
    assert [is_measured(v) for _w, v in series] == [True, False, False, True]


def test_trailing_gaps_are_suppressed_so_latest_stays_a_measurement():
    """A model last seen before the outage must not end its series on a
    sentinel: the model page prints ``values[-1]`` as "Latest"."""
    m = _manifest(
        weekly_model_only_in=("2026-W28",),  # biweekly stops before the gap
    )
    series = m.timeseries("p-one", "biweekly", "refusal_rate")
    assert series == [("2026-W28", 0.20)]


def test_a_pair_never_measured_gets_an_empty_series_not_gaps():
    m = _manifest()
    assert m.timeseries("p-one", "nonexistent", "refusal_rate") == []


def test_gap_sentinel_formats_as_a_dash_under_any_format_spec():
    """prompt.html renders each cell as ``spec.fmt.format(value)`` with
    spec.fmt a format string carried in the template. Formatting None
    would raise TypeError and fail the whole build."""
    for spec_fmt in ("{:.2f}", "{:.0f}", "{}"):
        assert spec_fmt.format(MISSING_WEEK) == "—"
    assert not MISSING_WEEK


# ---------------------------------------------------------------------
# Change-point indices survive the reindexing
# ---------------------------------------------------------------------


def test_change_point_indices_are_remapped_past_the_gap():
    """The pipeline computes change points over measurements only. Index
    2 means "the third measurement", which after gap insertion sits at
    position 4 of the series, not position 2."""
    cp = dict(_CHANGE_POINTS, refusal_rate=[2])
    m = _manifest(change_points=cp)
    series = m.timeseries("p-one", "weekly", "refusal_rate")
    idx = m.change_points_for("p-one", "weekly", "refusal_rate")
    assert idx == [4]
    assert series[4][0] == "2026-W32"
    assert is_measured(series[4][1])


def test_out_of_range_change_point_indices_are_dropped():
    cp = dict(_CHANGE_POINTS, refusal_rate=[0, 99])
    m = _manifest(change_points=cp)
    assert m.change_points_for("p-one", "weekly", "refusal_rate") == [0]


# ---------------------------------------------------------------------
# The line actually breaks
# ---------------------------------------------------------------------


def _polylines(svg: str) -> list[str]:
    return re.findall(r'<polyline[^>]*points="([^"]*)"', svg)


def test_sparkline_splits_into_segments_across_a_gap():
    svg = str(sparkline([0.1, 0.2, None, None, 0.3, 0.4]))
    assert len(_polylines(svg)) == 2, svg
    # The x positions of the surviving points stay on the calendar grid:
    # six slots across 140px, so the last point is still at x=140.
    assert _polylines(svg)[0].startswith("0.0,")
    assert _polylines(svg)[1].endswith("140.0,0.0")


def test_sparkline_draws_one_unbroken_line_when_nothing_is_missing():
    svg = str(sparkline([0.1, 0.2, 0.3, 0.4]))
    assert len(_polylines(svg)) == 1


def test_sparkline_draws_an_isolated_measurement_as_a_dot():
    """A single measurement between two gaps cannot be a line. Dropping
    it would hide a real number."""
    svg = str(sparkline([0.1, 0.2, None, 0.3, None, 0.4, 0.5]))
    assert svg.count("<circle") == 1
    assert len(_polylines(svg)) == 2


def test_sparkline_labels_the_gap_for_screen_readers():
    svg = str(sparkline([0.1, None, 0.3], label="Weekly"))
    assert "line breaks where no run happened" in svg
    assert "no run" in svg


def test_sparkline_with_only_gaps_is_no_data():
    assert "no data" in str(sparkline([None, None]))


def test_sparkline_gap_does_not_move_the_change_point_marker():
    svg = str(sparkline([0.1, 0.2, None, 0.4], change_point_indices=[3]))
    assert "(change-point marked)" in svg
    # 4 slots across 140px => step 46.7; index 3 lands on the right edge.
    assert 'x1="140.0"' in svg


def test_sparkline_ignores_booleans_as_measurements():
    """bool subclasses int; a stray True must not plot as 1.0. It is
    treated as a gap, leaving two isolated measurements."""
    svg = str(sparkline([0.1, True, 0.3]))
    assert _polylines(svg) == []
    assert svg.count("<circle") == 2


# ---------------------------------------------------------------------
# The gap is visible on /data/
# ---------------------------------------------------------------------


def _build(tmp_path: Path, manifest: dict) -> Path:
    manifest_path = tmp_path / f"manifest-{manifest['snapshot']['week_id']}.json"
    manifest_path.write_text(json.dumps(manifest))
    dist = tmp_path / "dist"
    result = subprocess.run(
        [
            "uv", "run", "python",
            str(REPO_ROOT / "site" / "src" / "build.py"),
            "--manifest", str(manifest_path),
            "--out", str(dist),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"site build failed: {result.stderr}"
    return dist


def test_data_index_shows_the_lost_weeks(tmp_path: Path):
    dist = _build(tmp_path, _manifest_dict())

    index = (dist / "data" / "index.html").read_text()
    for week in ("2026-W30", "2026-W31"):
        assert f"/data/{week}/" in index, f"{week} missing from the data index"
        # Labelled as a gap by the only file it carries, so the row reads
        # as a gap without opening it.
        assert f"/data/{week}/NO-DATA.md" in index

    # Every link the index row emits must resolve; a gap row that 404s
    # trades one invisible hole for a visible broken link.
    for week in ("2026-W30", "2026-W31"):
        assert (dist / "data" / week / "index.html").exists()
        assert (dist / "data" / week / "NO-DATA.md").exists()
        assert (dist / "data" / week / "SHA256SUMS").exists()

    urls = set((dist / "urls.txt").read_text().split())
    assert "/data/2026-W30/" in urls
    assert "/data/2026-W31/" in urls


def test_gap_week_page_says_there_is_no_data_and_no_backfill(tmp_path: Path):
    dist = _build(tmp_path, _manifest_dict())
    page = (dist / "data" / "2026-W30" / "index.html").read_text()
    assert "No data" in page
    assert "no snapshot" in page
    assert "Not backfilled" in page
    assert "/methodology/#data-gaps" in page
    # No metrics files are advertised for a week that has none.
    assert "metrics.csv" not in page


def test_gap_weeks_get_no_data_payload(tmp_path: Path):
    """A gap week must never carry a metrics file. An empty metrics.csv
    would read as "we measured and found nothing"."""
    dist = _build(tmp_path, _manifest_dict())
    for name in ("metrics.csv", "metrics.jsonl", "manifest.json"):
        assert not (dist / "data" / "2026-W30" / name).exists()


def test_contiguous_history_emits_no_gap_directories(tmp_path: Path):
    dist = _build(
        tmp_path,
        _manifest_dict(
            snapshot_week="2026-W29", history_weeks=("2026-W27", "2026-W28"),
        ),
    )
    assert not (dist / "data" / "2026-W30").exists()
    index = (dist / "data" / "index.html").read_text()
    assert "NO-DATA.md" not in index
