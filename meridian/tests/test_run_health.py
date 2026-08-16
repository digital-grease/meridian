"""Tests for scripts/check_run_health.py — the publish-time guard that
reports when a run recorded failed pairs, dead samples, or a missing week.

Two contracts are pinned here, and they pull in opposite directions:

1. A run that failed must never report clean. That is the original
   2026-W27 gap, where gpt-5.5 400'd on every zero-temperature request
   and the workflow stayed green.
2. A finding must not be louder than the thing it found. The check was a
   hard gate on a single dead sample until 2026-08-15, which is how
   2026-W32 (60/60 pairs, committed as 6d74411) ended up never deployed.

The verdict is a three-level `RunHealth`, not a boolean, precisely so
"noticed" and "blocked the pipeline" can be different answers.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_run_health.py"
_spec = importlib.util.spec_from_file_location("check_run_health", _SCRIPT)
crh = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(crh)


def _entry(week, *, complete, failed, errors=(), **kw):
    entry = {
        "week_id": week,
        "pairs_complete": complete,
        "pairs_failed": failed,
        "pairs_skipped": 0,
        "errors": list(errors),
        # A real week writes 1350 samples (30 prompts x 45 samples across
        # the on-cadence roster). The denominator matters now: the
        # unusable-sample check is a rate, not a truth test.
        "total_samples_written": 1350,
    }
    entry.update(kw)
    return entry


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
    verdict = crh.evaluate(_entry("2026-W26", complete=60, failed=0))
    assert verdict.level == "ok"
    assert verdict.ok is True


def test_failed_pairs_are_unhealthy():
    # Mirrors the real 2026-W27 gpt-5.5 temperature=0 400 storm.
    err = {
        "provider": "openai",
        "model_id": "gpt-5.5",
        "error_type": "UpstreamError",
        "message": "Error code: 400 - 'temperature' does not support 0 ...",
    }
    verdict = crh.evaluate(_entry("2026-W27", complete=30, failed=30, errors=[err]))
    assert verdict.level == "fail"
    assert verdict.ok is False
    assert "gpt-5.5" in verdict.detail and "30 failed pair" in verdict.detail


def test_errors_without_failed_count_still_unhealthy():
    verdict = crh.evaluate(
        _entry("2026-W27", complete=60, failed=0, errors=[{"message": "x"}])
    )
    assert verdict.ok is False


def test_multiline_error_message_is_collapsed_to_one_line():
    """A provider error with newlines in it must not be able to inject
    lines into $GITHUB_ENV. The 2026-W27 gpt-5.5 400 bodies were exactly
    this shape."""
    err = {"provider": "openai", "model_id": "gpt-5.5", "error_type": "UpstreamError",
           "message": "Error code: 400\nHEALTH_DETAIL=owned\nPATH=/tmp/evil"}
    verdict = crh.evaluate(_entry("2026-W27", complete=0, failed=30, errors=[err]))
    assert "\n" not in verdict.detail


def test_env_file_uses_the_heredoc_form(tmp_path, monkeypatch):
    """`key=value` cannot survive a value with a newline in it. The
    delimiter form can, and the value is collapsed on top of that."""
    env_file = tmp_path / "env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    crh._publish_detail("line one\nline two\tand   three")
    written = env_file.read_text(encoding="utf-8").splitlines()
    assert written[0].startswith("HEALTH_DETAIL<<")
    delimiter = written[0].split("<<", 1)[1]
    assert written[1] == "line one line two and three"
    assert written[2] == delimiter


def test_detail_is_exported_as_a_step_output_too(tmp_path, monkeypatch):
    """The alert job is a separate job now, and job environments do not
    cross job boundaries, so the finding also has to be a step output."""
    out = tmp_path / "out"
    out.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("GITHUB_ENV", raising=False)
    crh._publish_detail("something happened")
    assert out.read_text(encoding="utf-8").startswith("health_detail<<")


def test_publish_detail_is_a_no_op_outside_actions(monkeypatch):
    """Run locally there is no env file to write to; that must not raise."""
    monkeypatch.delenv("GITHUB_ENV", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    crh._publish_detail("nothing to write to")


# --- cadence contiguity -------------------------------------------------
#
# `latest_entry_for_week` only ever looks at the target week, so a week
# that never ran is invisible to it. That is why the 2026-W30/W31 outage
# produced no signal from this script for two consecutive weeks.


def test_contiguous_history_reports_no_gaps():
    entries = [_entry(f"2026-W{n}", complete=60, failed=0) for n in (26, 27, 28, 29)]
    assert crh.missing_weeks(entries, "2026-W29") == []
    assert crh.cadence_health(entries, "2026-W29").level == "ok"


def test_missing_weeks_are_named():
    entries = [_entry(w, complete=60, failed=0)
               for w in ("2026-W28", "2026-W29", "2026-W32", "2026-W33")]
    assert crh.missing_weeks(entries, "2026-W33") == ["2026-W30", "2026-W31"]


def test_gap_touching_the_target_week_fails():
    """W31 never ran and W32 is the first run back: the cadence is broken
    right now and this is the first chance to say so."""
    entries = [_entry(w, complete=60, failed=0)
               for w in ("2026-W28", "2026-W29", "2026-W32")]
    verdict = crh.cadence_health(entries, "2026-W32")
    assert verdict.level == "fail"
    assert "2026-W31" in verdict.detail


def test_historical_gap_warns_but_does_not_fail():
    """2026-W30/W31 are permanently lost. Failing on them every week for
    the rest of the project's life is how alerts stop being read."""
    entries = [_entry(w, complete=60, failed=0)
               for w in ("2026-W28", "2026-W29", "2026-W32", "2026-W33")]
    verdict = crh.cadence_health(entries, "2026-W33")
    assert verdict.level == "warn"
    assert verdict.ok is True
    assert "2026-W30" in verdict.detail and "2026-W31" in verdict.detail


def test_first_run_and_empty_log_do_not_report_a_gap():
    """Early history is allowed to be sparse, and a first run has nothing
    to be contiguous with."""
    assert crh.missing_weeks([], "2026-W17") == []
    assert crh.missing_weeks([_entry("2026-W17", complete=30, failed=0)], "2026-W17") == []
    assert crh.cadence_health([], "2026-W17").level == "ok"


def test_unparseable_week_ids_are_skipped_not_raised():
    """Retention is forever and the log has hand-written lines in it."""
    entries = [
        {"week_id": "backfill-manual"},
        {"week_id": None},
        {},
        _entry("2026-W28", complete=60, failed=0),
        _entry("2026-W29", complete=60, failed=0),
    ]
    assert crh.missing_weeks(entries, "2026-W29") == []


def test_cadence_walk_crosses_a_year_boundary():
    """2026 is a 53-week ISO year. Walking Monday to Monday gets this
    right where incrementing the week number would not."""
    entries = [_entry("2026-W52", complete=60, failed=0),
               _entry("2027-W02", complete=60, failed=0)]
    assert crh.missing_weeks(entries, "2027-W02") == ["2026-W53", "2027-W01"]


def test_far_future_target_is_bounded():
    """A typo'd week must report a bounded problem, not spin."""
    entries = [_entry("2026-W28", complete=60, failed=0),
               _entry("2026-W29", complete=60, failed=0)]
    gaps = crh.missing_weeks(entries, "2099-W01")
    assert len(gaps) <= crh.MAX_WEEKS_SCANNED


def test_weeks_after_the_target_are_ignored():
    """Re-publishing an old week must not be told the future is missing."""
    entries = [_entry(w, complete=60, failed=0)
               for w in ("2026-W27", "2026-W28", "2026-W32")]
    assert crh.missing_weeks(entries, "2026-W28") == []


# --- composition and exit codes ----------------------------------------


def test_combine_takes_the_worst_level_and_keeps_every_detail():
    verdict = crh.combine(
        crh.RunHealth("warn", "dead samples"),
        crh.RunHealth("fail", "missing week"),
    )
    assert verdict.level == "fail"
    assert "dead samples" in verdict.detail and "missing week" in verdict.detail


def test_exit_codes_are_three_distinct_channels():
    """0 / 3 / 1, and specifically not 0 for both clean and warn.

    run-weekly.sh picks the EC2 run's SNS subject from this status. While
    warn shared 0 with clean, its "completed with warnings" subject was
    unreachable and the 2026-08-10 run, 20 empty samples inside tolerance,
    emailed "weekly run succeeded".
    """
    assert crh.EXIT_CLEAN == 0
    assert crh.EXIT_WARN == 3
    assert crh.EXIT_FAIL == 1
    assert len({crh.EXIT_CLEAN, crh.EXIT_WARN, crh.EXIT_FAIL}) == 3


def test_main_exit_codes(tmp_path):
    log = tmp_path / "run_log.jsonl"
    import json
    log.write_text(
        json.dumps(_entry("2026-W26", complete=60, failed=0)) + "\n"
        + json.dumps(_entry("2026-W27", complete=30, failed=30, errors=[{"message": "x"}])) + "\n",
        encoding="utf-8",
    )
    assert crh.main(["2026-W26", "--run-log", str(log)]) == crh.EXIT_CLEAN
    assert crh.main(["2026-W27", "--run-log", str(log)]) == crh.EXIT_FAIL
    assert crh.main(["2026-W99", "--run-log", str(log)]) == crh.EXIT_FAIL  # missing week


def test_main_missing_run_log(tmp_path):
    assert crh.main(["2026-W27", "--run-log", str(tmp_path / "nope.jsonl")]) == crh.EXIT_FAIL


def test_main_does_not_fail_on_a_historical_gap(tmp_path, capsys):
    """The 2026-W33 case: W30/W31 are gone forever, W32 published, and
    Monday's run must not go red over it.

    Exit 3, not 0: the caller has to be able to tell "clean" from "worth
    a look" without re-parsing stdout. Not 1 either, because a red health
    job is what kept 2026-W32 off the public site.
    """
    import json
    log = tmp_path / "run_log.jsonl"
    log.write_text(
        "".join(
            json.dumps(_entry(w, complete=60, failed=0)) + "\n"
            for w in ("2026-W28", "2026-W29", "2026-W32", "2026-W33")
        ),
        encoding="utf-8",
    )
    assert crh.main(["2026-W33", "--run-log", str(log)]) == crh.EXIT_WARN
    out = capsys.readouterr().out
    # Silent tolerance is not tolerance, it is a blind spot.
    assert "::warning" in out
    assert "::error" not in out
    assert "2026-W30" in out
    # The gap must not swallow the description of the run that did happen.
    assert "pairs_complete=60" in out
    assert "total_samples=1350" in out


def test_the_run_summary_survives_a_warning(tmp_path, capsys):
    """`combine` used to drop every `ok` detail, and `evaluate` returns the
    run summary as an `ok` detail. Because 2026-W30/W31 are permanently
    missing, that meant the one line describing the run's shape was
    discarded on every week from 2026-W33 onward: no pairs, no sample
    counts, in the step summary, the annotation, stdout, or the SNS body
    run-weekly.sh builds from stdout."""
    summary = crh.RunHealth(
        "ok", "week=2026-W33 pairs_complete=60 total_samples=1350"
    )
    combined = crh.combine(summary, crh.RunHealth("warn", "run_log gap"))
    assert combined.level == "warn"
    assert "pairs_complete=60" in combined.detail
    assert "run_log gap" in combined.detail
    # Worst first, so the actionable half leads the SNS subject line.
    assert combined.detail.index("run_log gap") < combined.detail.index("week=")


def test_a_permanent_gap_stops_being_announced_once_it_leaves_the_window():
    """A warning that repeats forever with an unchangeable message is the
    pager-fatigue mechanism this module exists to avoid (issues #24, #26).

    2026-W30/W31 cannot be backfilled, so from 2026-W33 the old unbounded
    walk warned every week for the life of the project. The walk is now
    bounded to CADENCE_WINDOW_WEEKS before the target: loud while the gap
    is news, silent once it is only history.
    """
    entries = [_entry(w, complete=60, failed=0)
               for w in ("2026-W28", "2026-W29")]
    entries += [_entry(f"2026-W{n}", complete=60, failed=0) for n in range(32, 53)]
    # W33 is three weeks after the outage: still inside the window.
    assert crh.cadence_health(entries, "2026-W33").level == "warn"
    # W44 is thirteen weeks after it: outside the window, and by then the
    # gap has been reported on eleven consecutive runs.
    assert crh.missing_weeks(entries, "2026-W44") == []
    assert crh.cadence_health(entries, "2026-W44").level == "ok"


def test_a_new_hole_older_than_the_window_is_impossible_but_a_recent_one_is_caught():
    """The window only ever removes re-announcements. The run_log is
    append-only and weeks publish in order, so a hole appears adjacent to
    the target week, which is always inside the window."""
    entries = [_entry(w, complete=60, failed=0)
               for w in ("2026-W28", "2026-W29", "2026-W30", "2026-W32", "2026-W33")]
    # W31 went missing the week it happened, six weeks back from W37.
    assert crh.missing_weeks(entries + [
        _entry(f"2026-W{n}", complete=60, failed=0) for n in (34, 35, 36, 37)
    ], "2026-W37") == ["2026-W31"]


# --- malformed run_log lines --------------------------------------------


def test_a_malformed_run_log_line_degrades_instead_of_crashing(tmp_path, capsys):
    """`json.loads` was called bare, so one truncated or hand-edited line
    killed the process before it wrote $GITHUB_OUTPUT. The alert job then
    saw an empty health_detail and reported the wrong half of the pipeline
    as broken. Retention is forever and the log has hand-written lines in
    it, so this is a shape that exists."""
    import json
    log = tmp_path / "run_log.jsonl"
    log.write_text(
        json.dumps(_entry("2026-W28", complete=60, failed=0)) + "\n"
        + '{"week_id": "2026-W2\n'                      # truncated mid-write
        + json.dumps(_entry("2026-W29", complete=60, failed=0)) + "\n"
        + '["not", "an", "entry"]\n',                   # valid JSON, wrong shape
        encoding="utf-8",
    )
    rc = crh.main(["2026-W29", "--run-log", str(log)])
    assert rc == crh.EXIT_WARN
    out = capsys.readouterr().out
    assert "unparseable line" in out
    # The verdict for the target week is still computed and still reported.
    assert "pairs_complete=60" in out


def test_read_entries_reports_the_line_numbers_it_skipped(tmp_path):
    log = tmp_path / "run_log.jsonl"
    log.write_text('{"week_id": "2026-W28"}\nnot json\n\n42\n', encoding="utf-8")
    entries, malformed = crh._read_entries(str(log))
    assert [e["week_id"] for e in entries] == ["2026-W28"]
    assert malformed == [2, 4]  # blank line 3 is not a defect
