#!/usr/bin/env python3
"""Report a published week's run health: failed pairs, dead samples, gaps.

Context: the weekly-pipeline publish job pulls the raw artifacts (manifest,
snapshot, run_log) from S3 and commits them to ``main`` (append-only
retention is a hard rule). Historically the job only went red when the S3
*sync* itself failed, so a run that sampled but recorded failed pairs,
2026-W27 for instance, where ``gpt-5.5`` 400'd on every zero-temperature
request, published as a green "success" with nothing surfacing the failure.

This script closed that gap, and then opened a worse one. Until 2026-08-15
it ran as a step *inside* the publish job, so any finding it made turned the
whole workflow red, and weekly-build.yml gated its deploy on
``workflow_run.conclusion == 'success'``. 2026-W32 sampled cleanly (60/60
pairs), committed to main as 6d74411, and never reached meridianaudit.org,
because this script exited 1 over a data-quality observation and the site
build was skipped. The public dashboard served 2026-W29 for three weeks.
A verdict about the *contents* of a week must never decide whether that
week gets published, so the script now runs in its own ``health`` job that
alerts without blocking the deploy.

Three checks, most severe first:

1. **Failed pairs / recorded errors.** Requests that did not succeed.
   Always a failure.

2. **Unusable samples**, meaning requests that returned 200 with nothing
   measurable in them. Until 2026-08-15 this was a bare truth test, so a
   single dead sample out of ~1350 turned the run red. That is not a
   useful signal: ``gpt-5.5`` returned 43 to 47 empty bodies per 600
   samples for weeks on end, and an operator who is paged for every one of
   them stops reading the page (see issues #24 and #26, both opened on
   schedule during the 2026-W30/W31 outage, both unread for two weeks).
   The check now fails only when the loss is big enough to damage the
   measurement, and warns otherwise.

3. **Cadence contiguity.** ``latest_entry_for_week`` only ever looks at the
   target week, so a week that never ran at all is invisible to it: that is
   precisely why the 2026-W30 and 2026-W31 total outage (EC2
   InsufficientInstanceCapacity, data permanently lost) produced no signal
   from this script for two consecutive weeks. The run_log's weeks are now
   walked for holes at the expected weekly cadence, over a bounded recent
   window rather than the whole log, so a permanent gap is announced while
   it is news and then stops (see ``CADENCE_WINDOW_WEEKS``).

Usage:
    check_run_health.py <ISO-WEEK> [--run-log PATH]

Exit code is the operator-facing verdict, and only that:
    0 = clean
    3 = a warning: reported everywhere a failure is, damages nothing, and
        must not turn a caller red
    1 = a finding that needs a human

A warning gets its own code rather than sharing 0 because every caller
that reads this verdict reads it as an exit status. scripts/run-weekly.sh
picks the EC2 run's SNS subject from it, and while warn returned 0 its
"completed with warnings" branch was unreachable: the 2026-08-10 run, 20
empty samples and comfortably inside tolerance, emailed "weekly run
succeeded". The publish workflow maps 3 back onto a green job so a
tolerated loss still cannot block the site deploy.

When ``$GITHUB_STEP_SUMMARY`` / ``$GITHUB_ENV`` / ``$GITHUB_OUTPUT`` are
present (i.e. running under Actions) it writes a run summary, exports the
finding as both ``HEALTH_DETAIL`` and the ``health_detail`` step output, and
emits a ``::error::`` / ``::warning::`` annotation. Run locally it just
prints. The step output exists because the alert job is a separate job now,
and job environments do not cross job boundaries.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from datetime import date, timedelta
from typing import NamedTuple

# Fail when more than this fraction of the run's stored samples came back
# with nothing measurable in them. 0.5% of a 1350-sample week is six
# samples, so the seventh fails: roughly a third of one prompt-model cell
# at N=20, enough to notice and not enough to invalidate the cell's
# statistics.
#
# The value is pinned directly in meridian/tests/test_run_health_unusable.py
# rather than only bracketed by behaviour, because widening it is a
# decision about how much of a week may go unmeasured.
UNUSABLE_FAIL_FRACTION = 0.005

# Hard ceiling on iterations of the cadence walk. ``CADENCE_WINDOW_WEEKS``
# already bounds it; this stays as the guard that does not depend on the
# window being sane, so a typo'd or far-future target week ("2099-W01")
# reports a bounded problem instead of spinning.
MAX_WEEKS_SCANNED = 520

# How far back from the target week the cadence walk looks. A quarter.
#
# The walk used to start at the log's first week, which meant a gap was
# re-announced on every subsequent run for the life of the project.
# 2026-W30 and 2026-W31 are permanently missing, so from 2026-W33 onward
# every single week would have warned, with an identical and unactionable
# message, which is the exact pager-fatigue mechanism issues #24 and #26
# demonstrate: both were opened on schedule during that outage and neither
# was read for two weeks.
#
# Bounding the window costs nothing in detection. The log is append-only
# and weeks publish in order, so a hole appears at the moment it happens,
# always adjacent to the target week and always inside any window. What
# the bound removes is only the re-announcement, and a quarter is long
# enough that the gap is still visible while it affects the reports being
# written about those weeks.
CADENCE_WINDOW_WEEKS = 12

# The operator-facing verdict, as an exit status. Callers branch on these.
#
# EXIT_WARN is deliberately not 0. scripts/run-weekly.sh chooses the EC2
# run's SNS subject from this code, and while a warning shared 0 with a
# clean run its "completed with warnings" subject could never be selected:
# the 2026-08-10 run had 20 empty samples, well inside tolerance, and
# emailed "weekly run succeeded". EXIT_WARN is also not 1, because a
# tolerated loss must not turn the publish workflow's health job red, which
# is the coupling that kept 2026-W32 off meridianaudit.org for three weeks.
# It needs its own channel, so it has one.
EXIT_CLEAN = 0
EXIT_FAIL = 1
EXIT_WARN = 3

_ISO_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


class RunHealth(NamedTuple):
    """One verdict about a run.

    ``level`` is ``"ok"``, ``"warn"`` or ``"fail"``. A warning is reported
    everywhere a failure is (annotation, step summary, ``HEALTH_DETAIL``)
    and simply does not change the exit code, so the operator still learns
    about it without the workflow going red.
    """

    level: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.level != "fail"


_SEVERITY = {"ok": 0, "warn": 1, "fail": 2}


def _one_line(value: str) -> str:
    """Collapse every run of whitespace to a single space.

    ``$GITHUB_ENV`` and ``$GITHUB_OUTPUT`` are line-oriented files. A
    provider error containing a newline, exactly the shape of the 2026-W27
    ``gpt-5.5`` 400 bodies, would otherwise inject arbitrary extra lines
    into the environment file and corrupt every variable after it.
    """
    return " ".join(str(value).split())


def latest_entry_for_week(entries: list[dict], week: str) -> dict | None:
    """Return the most recent run_log entry for ``week`` (last wins), or
    None if the week never ran."""
    match = None
    for rec in entries:
        if rec.get("week_id") == week:
            match = rec
    return match


def _monday(week_id: object) -> date | None:
    """Monday of an ISO week id like ``2026-W32``, or None if unparseable.

    Retention is forever and the log has hand-written lines in it, so a
    week id that does not parse is skipped rather than raised on.
    """
    if not isinstance(week_id, str):
        return None
    m = _ISO_WEEK_RE.match(week_id)
    if not m:
        return None
    try:
        return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError:
        # Week 53 in a 52-week ISO year, month/day out of range, etc.
        return None


def _week_id(day: date) -> str:
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def missing_weeks(entries: list[dict], target: str) -> list[str]:
    """Weeks with no run_log entry in the window ending at ``target``.

    Walks Monday to Monday rather than incrementing the week number, so
    year boundaries (2026-W52 to 2027-W01) and 53-week ISO years are
    handled by the calendar instead of by arithmetic.

    The walk starts at whichever is later: the week after the log's first
    week, or ``CADENCE_WINDOW_WEEKS`` before the target. Starting at the
    log's first week meant a permanent hole was re-reported on every run
    forever; see the constant for why that is worse than not reporting it.

    Returns an empty list rather than raising when there is nothing to
    check: an empty log, a first run, an unparseable target, or a target
    that predates everything on record. Early history is allowed to be
    sparse and this must never be the reason a publish reports a problem.
    """
    target_monday = _monday(target)
    if target_monday is None:
        return []

    seen: set[date] = set()
    for rec in entries:
        monday = _monday(rec.get("week_id"))
        if monday is not None and monday <= target_monday:
            seen.add(monday)

    if len(seen) < 2:
        # Nothing to be contiguous *with*.
        return []

    gaps: list[str] = []
    window_start = target_monday - timedelta(days=7 * CADENCE_WINDOW_WEEKS)
    cursor = max(min(seen) + timedelta(days=7), window_start)
    scanned = 0
    while cursor < target_monday and scanned < MAX_WEEKS_SCANNED:
        if cursor not in seen:
            gaps.append(_week_id(cursor))
        cursor += timedelta(days=7)
        scanned += 1
    return gaps


def cadence_health(entries: list[dict], target: str) -> RunHealth:
    """Assert the weekly cadence is unbroken up to ``target``.

    A gap that touches the target week means the cadence is broken *now*:
    last week did not run and this is the first evidence of it. That fails.

    An older gap is a historical fact that was already alerted on when it
    happened. 2026-W30 and 2026-W31 are permanently missing, the instances
    never started and the data cannot be recovered, so failing on them
    every week for the remaining life of the project would put the build
    permanently red and teach the operator to ignore it. Those warn, and
    they warn only while they are inside ``CADENCE_WINDOW_WEEKS`` of the
    target: a warning nobody can ever act on and nobody can ever clear is
    not a warning, it is wallpaper.
    """
    gaps = missing_weeks(entries, target)
    if not gaps:
        return RunHealth("ok", f"cadence contiguous through {target}")

    names = ", ".join(gaps)
    target_monday = _monday(target)
    previous = _week_id(target_monday - timedelta(days=7)) if target_monday else None

    if previous is not None and previous in gaps:
        detail = (
            f"the run_log has no entry for {previous}, the week immediately "
            f"before {target}: the weekly cadence is broken and that week's "
            f"data does not exist. Missing week(s): {names}. Check the "
            f"orchestrator Lambda logs for the missing week(s) "
            f"(aws logs tail /aws/lambda/meridian-orchestrator --region "
            f"us-east-2) and record the gap; there is no backfill."
        )
        return RunHealth("fail", detail)

    detail = (
        f"run_log gap: no entry for {names}, before {target}. Already "
        f"historical, so this does not fail the check, but every "
        f"week-over-week comparison across the gap is comparing "
        f"non-adjacent weeks and must say so."
    )
    return RunHealth("warn", detail)


def _total_samples(entry: dict) -> int:
    """Denominator for the unusable-sample rate.

    ``total_samples_written`` has been on every run_log entry since the log
    existed; the per-runner sum is the fallback for a hand-written or
    partially reconstructed line.
    """
    total = entry.get("total_samples_written")
    if isinstance(total, int) and total > 0:
        return total
    per_runner = entry.get("per_runner_samples") or {}
    try:
        return sum(int(v) for v in per_runner.values())
    except (TypeError, ValueError):
        return 0


def lost_cells(entry: dict) -> list[str]:
    """Prompt-model cells where every sample came back unmeasurable.

    A cell that loses all N samples is not a rate problem, it is a hole in
    the corpus: that (prompt, model, week) has no measurement at all, and
    no tolerance should ever forgive it.

    Reads the optional ``unusable_cells`` field, a list of
    ``{"runner": "...", "prompt_id": "...", "unusable": N, "samples": N}``.
    ``unusable_samples`` alone cannot answer this question, because it is
    aggregated per runner and per reason with no prompt dimension: 43 dead
    samples spread thinly over 30 prompts and 20 dead samples concentrated
    in one prompt are the same number there. When the field is absent this
    returns nothing and the rate check below is the only tolerance in play.
    """
    cells = entry.get("unusable_cells") or []
    if not isinstance(cells, list):
        return []
    lost: list[str] = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        try:
            samples = int(cell.get("samples", 0) or 0)
            unusable = int(cell.get("unusable", 0) or 0)
        except (TypeError, ValueError):
            continue
        if samples > 0 and unusable >= samples:
            runner = cell.get("runner", "?")
            prompt_id = cell.get("prompt_id", "?")
            lost.append(f"{runner} x {prompt_id} ({unusable}/{samples})")
    return sorted(lost)


def _unusable_breakdown(unusable: dict) -> str:
    return "; ".join(
        f"{runner} " + ", ".join(f"{r}={c}" for r, c in sorted(reasons.items()))
        for runner, reasons in sorted(unusable.items())
    )


def _unusable_detail(n: int, week: str, breakdown: str, verdict: str) -> str:
    """Shared body for every unusable-sample verdict.

    Kept in one place so the warn and fail wordings cannot drift apart:
    the operator needs the same diagnosis either way, only the severity
    sentence differs.
    """
    return (
        f"{n} sample(s) in {week} returned no usable content "
        f"({breakdown}). {verdict} They are stored but excluded from every "
        f"metric, so affected cells are under-sampled. A 'truncated-empty' "
        f"reason means the model exhausted its completion budget without "
        f"emitting output: raise that runner's max_tokens in "
        f"meridian/config.yaml."
    )


def evaluate(entry: dict) -> RunHealth:
    """Return the health verdict for a single run_log entry.

    Unhealthy when any pair failed, any error was recorded, a whole
    prompt-model cell lost every sample, or unusable samples exceed
    ``UNUSABLE_FAIL_FRACTION`` of the run. A smaller number of unusable
    samples warns. A run that merely *skips* on-cadence batches (e.g. a
    thinking-default model that only exposes the API-default temperature,
    so the zero-temp batch is skipped) is healthy: skips don't increment
    ``pairs_failed``.

    The unusable-sample check was added 2026-07-24. Until then this
    function only read ``pairs_failed``/``errors``, which are request
    *failures*, so gpt-5.5 returning HTTP 200 with an empty body on 43 of
    600 samples passed as a clean run for two consecutive weeks. A request
    that succeeds and returns nothing is not an error by any transport
    measure and still has to reach a human. It was given a tolerance on
    2026-08-15, for the opposite reason: as a hard gate it fired on a
    single dead sample, which is noise at N=20 per cell.
    """
    failed = int(entry.get("pairs_failed", 0) or 0)
    errors = entry.get("errors") or []
    unusable = entry.get("unusable_samples") or {}
    n_unusable = sum(sum(v.values()) for v in unusable.values())
    week = entry.get("week_id", "?")
    total = _total_samples(entry)
    summary = (
        f"week={week} pairs_complete={entry.get('pairs_complete')} "
        f"pairs_failed={failed} pairs_skipped={entry.get('pairs_skipped')} "
        f"errors={len(errors)} unusable_samples={n_unusable} "
        f"total_samples={total}"
    )

    if failed > 0 or errors:
        first = errors[0] if errors else {}
        msg = _one_line(first.get("message") or "")[:200]
        detail = (
            f"{failed} failed pair(s) in {week}. "
            f"first error: {first.get('provider')}/{first.get('model_id')} "
            f"{first.get('error_type')}: {msg}"
        )
        return RunHealth("fail", detail)

    if not n_unusable:
        return RunHealth("ok", summary)

    breakdown = _unusable_breakdown(unusable)

    lost = lost_cells(entry)
    if lost:
        return RunHealth(
            "fail",
            _unusable_detail(
                n_unusable,
                week,
                breakdown,
                f"{len(lost)} prompt-model cell(s) lost every sample "
                f"({'; '.join(lost)}), so that cell has no measurement for "
                f"this week at all.",
            ),
        )

    if total <= 0:
        return RunHealth(
            "fail",
            _unusable_detail(
                n_unusable,
                week,
                breakdown,
                "The entry records no sample total, so the loss rate cannot "
                "be computed and cannot be shown to be within tolerance.",
            ),
        )

    fraction = n_unusable / total
    pct = f"{fraction * 100:.2f}%"
    limit = f"{UNUSABLE_FAIL_FRACTION * 100:.2f}%"
    if fraction > UNUSABLE_FAIL_FRACTION:
        return RunHealth(
            "fail",
            _unusable_detail(
                n_unusable,
                week,
                breakdown,
                f"That is {pct} of the {total} samples written, over the "
                f"{limit} tolerance.",
            ),
        )
    return RunHealth(
        "warn",
        _unusable_detail(
            n_unusable,
            week,
            breakdown,
            f"That is {pct} of the {total} samples written, within the "
            f"{limit} tolerance, so it is reported and not failed.",
        ),
    )


def combine(*verdicts: RunHealth) -> RunHealth:
    """Fold several verdicts into one, worst level wins, worst detail first.

    *Every* detail is kept, including the ``ok`` ones. An operator looking
    at a week with both a cadence gap and dead samples needs to see both,
    not whichever one the code checked first, and they need the run's shape
    alongside either.

    Dropping the ``ok`` details is what this used to do, and it cost the
    only line that describes the run. ``evaluate`` returns the summary
    ("week=... pairs_complete=60 pairs_failed=0 ... total_samples=1350") as
    an ``ok`` detail, so on any week where the cadence check warned, which
    from 2026-W33 is every week, that summary was discarded before it
    reached the step summary, the annotation, stdout, or the SNS body
    run-weekly.sh builds out of stdout. The verdict said what was wrong and
    nothing said what the run was.

    Details are ordered by descending severity so the actionable half leads
    and the summary trails it. ``sorted`` is stable, so verdicts of equal
    severity keep the order they were passed in.
    """
    worst = max(verdicts, key=lambda v: _SEVERITY[v.level])
    ordered = sorted(verdicts, key=lambda v: -_SEVERITY[v.level])
    parts = [v.detail for v in ordered if v.detail]
    return RunHealth(worst.level, " | ".join(parts))


def _read_entries(path: str) -> tuple[list[dict], list[int]]:
    """Parse the run_log, returning ``(entries, malformed line numbers)``.

    A line that will not parse is skipped rather than raised on. This used
    to call ``json.loads`` bare, which made one truncated or hand-edited
    line, and retention is forever so hand-edited lines exist, an uncaught
    ``JSONDecodeError``: the process died before writing ``$GITHUB_OUTPUT``,
    so the alert job saw an empty ``health_detail`` and reported the wrong
    half of the pipeline as broken. Degrading here keeps the verdict about
    the target week computable, and the skipped lines are reported so the
    corruption is still visible.

    Line numbers are 1-based, matching what an operator sees in an editor.
    """
    entries: list[dict] = []
    malformed: list[int] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                malformed.append(lineno)
                continue
            if isinstance(record, dict):
                entries.append(record)
            else:
                # A bare list or string is syntactically fine and still not
                # a run_log entry; every reader here calls .get() on it.
                malformed.append(lineno)
    return entries, malformed


def _emit(level: str, annotation_title: str, message: str) -> None:
    """Emit a GitHub Actions annotation (harmless plain text locally)."""
    print(f"::{level} title={annotation_title}::{_one_line(message)}")


def _write_kv(env_var: str, key: str, value: str) -> None:
    """Append ``key=value`` to a GitHub Actions file-command file.

    Uses the heredoc form with a random delimiter. The plain ``key=value``
    form this replaced was a corruption waiting to happen: a multi-line
    provider error written straight into ``$GITHUB_ENV`` makes every line
    after the first a new (and probably invalid) variable assignment. The
    value is collapsed to one line as well, so the delimiter form is belt
    and braces rather than the only defence.
    """
    path = os.environ.get(env_var)
    if not path:
        return
    delimiter = f"MERIDIAN_{key.upper()}_{uuid.uuid4().hex}"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{key}<<{delimiter}\n{_one_line(value)}\n{delimiter}\n")


def _publish_detail(detail: str) -> None:
    """Export the finding to the rest of the workflow.

    ``HEALTH_DETAIL`` is read by later steps in this job; ``health_detail``
    is a step output because the SNS and issue steps live in a separate
    ``alert`` job now, and a job's environment does not cross into another
    job. Both are written for warnings as well as failures.
    """
    _write_kv("GITHUB_ENV", "HEALTH_DETAIL", detail)
    _write_kv("GITHUB_OUTPUT", "health_detail", detail)


def _write_summary(level: str, text: str) -> None:
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        heading = {"ok": "clean", "warn": "warning", "fail": "FAILURE"}[level]
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write(f"### Pipeline run health ({heading})\n\n{text}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("week", help="ISO week id, e.g. 2026-W27")
    ap.add_argument("--run-log", default="data/run_log.jsonl")
    args = ap.parse_args(argv)

    try:
        entries, malformed = _read_entries(args.run_log)
    except FileNotFoundError:
        detail = f"run_log not found at {args.run_log}"
        _emit("error", "Pipeline health", detail)
        _publish_detail(detail)
        return EXIT_FAIL

    entry = latest_entry_for_week(entries, args.week)
    if entry is None:
        detail = (
            f"no run_log entry for {args.week}: the artifacts published but "
            f"the run that produced them is not in the log."
        )
        _emit("error", "Pipeline health", detail)
        _publish_detail(detail)
        return EXIT_FAIL

    verdicts = [evaluate(entry), cadence_health(entries, args.week)]
    if malformed:
        lines = ", ".join(str(n) for n in malformed)
        verdicts.append(RunHealth(
            "warn",
            f"{len(malformed)} unparseable line(s) in {args.run_log} "
            f"(line {lines}) were skipped. The verdict below is computed "
            f"from the rest of the log, so a week recorded on one of those "
            f"lines is invisible to the cadence check until the line is "
            f"repaired. Raw data is append-only: fix the line, never drop it.",
        ))

    verdict = combine(*verdicts)
    _write_summary(verdict.level, verdict.detail)

    if verdict.level == "fail":
        _emit("error", "Pipeline run had failures", verdict.detail)
        _publish_detail(verdict.detail)
        return EXIT_FAIL

    if verdict.level == "warn":
        _emit("warning", "Pipeline run health", verdict.detail)
        _publish_detail(verdict.detail)
        print(f"run healthy with warnings: {verdict.detail}")
        return EXIT_WARN

    print(f"run healthy: {verdict.detail}")
    return EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
