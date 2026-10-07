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

4. **Expected roster.** Every runner that was due this week must have
   produced at least half of the samples it owed, judged per runner and
   never on the week's total: on 2026-W36 llama's 750 samples made the
   week look three-fifths full while Anthropic had written 45 of 1200. A
   week with no samples at all fails outright. Rows written before
   ``expected_samples`` existed get an expectation derived from the
   roster they did run and their pair counts.

5. **Stance.** When the week's manifest is present, a model whose every
   stance-bearing cell is ``na`` at confidence 0.0 had no working
   classifier, which is how 2026-W36 to W38 lost stance for every model
   without a signal anywhere.

All of a week's run_log entries are read, not just the last one. A
resumed or partial re-run records only what it did itself, so judging
the last entry alone could pass a week on a re-run that found every pair
already stored and wrote nothing.

Usage:
    check_run_health.py <ISO-WEEK> [--run-log PATH] [--manifest PATH]
                        [--gaps PATH] [--title-file PATH]

``--manifest`` defaults to ``manifests/<week>.json`` and ``--gaps`` to
``gaps.jsonl``, both next to the run_log. Either may be absent: without
the manifest the stance check is skipped and unusable samples are read
from the run_log alone; without the ledger no gap is acknowledged.

The gap ledger (``data/gaps.jsonl``)
------------------------------------
One JSON object per line, appended by hand when a week is known to be
lost, and append-only like everything else under ``data/``::

    {"week_id": "2026-W34", "reason": "killed at the SSM 3600s timeout",
     "recorded_at": "2026-08-31", "scope": "all"}

``week_id`` is required; ``reason`` is required in spirit (a gap nobody
can explain is not acknowledged). ``scope`` is ``"all"`` (the default,
the whole week is missing), a ``"provider/model"`` key, a bare provider,
or ``"stance"``. ``kind`` is ``lost``, ``partial``, ``degraded`` or
``note`` (absent means ``lost``). A ``lost`` or ``partial`` record for
the target week itself acknowledges the runners its scope covers, so a
coverage shortfall or failed pairs on those runners warn instead of
failing, and a ``"stance"`` record does the same for a dead classifier
(see :func:`acknowledged_runners`); this is what lets a week published
after the fact as a disclosed partial week, 2026-W34, publish without
paging. Any other field is ignored here. The site build (``site/src/data_coverage.py``)
reads the same lines for ``/data/coverage/`` and ``/methodology/#data-gaps``
and also reads ``kind`` (``lost``, ``partial``, ``degraded`` or ``note``;
absent means ``lost``), ``evidence`` (a list such as ``["issue #36",
"commit de2ec54"]``), ``runners`` (the roster due in a week with no
run_log entry), and two more scopes: a bare provider and ``"stance"``. An acknowledged gap in the week immediately
before the target downgrades from failure to warning, because the alert
has already been answered; an acknowledged older gap is not re-announced.
A line that does not parse is skipped and reported.

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

Alongside ``health_detail`` it writes ``health_title`` (the alert's
subject line, e.g. ``PAGE 2026-W38 anthropic BILLING: 0/1200 samples``,
labelled with the ISO week sampled rather than the date the alert went
out), ``health_fingerprint`` (week, subject and class, so a repeat of the
same finding comments on the open issue instead of opening another),
``health_data_loss`` (``true`` when an on-cadence runner lost most of its
data) and ``health_report``, the same findings one per line.
``--title-file`` writes the title to a file for scripts/run-weekly.sh.
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

# An on-cadence runner that produced less than this share of the samples
# it owed fails the week. Judged per runner: the week's total hides a dead
# provider behind a healthy one (2026-W36: 795 of 1950 overall, 45 of 1200
# for Anthropic). Half is far below any loss a working provider has shown,
# the content-policy blocks on ref-wifi-unauthorized cost gpt-5.5 at most
# 17 of 600, and far above anything a billing, auth or capacity failure
# leaves behind.
COVERAGE_FAIL_FRACTION = 0.5

# Content-policy rejections are counted as measurements and normally say
# nothing louder than a line in the summary. Past this share of one
# runner's expected samples they warn: ref-wifi-unauthorized alone has
# cost gpt-5.5 between 4 and 17 of 600, so 10% is a policy shift, not a
# boundary prompt.
REJECTION_WARN_FRACTION = 0.1

# Samples per pair assumed for run_log rows written before
# ``expected_samples`` was recorded. The default-temperature batch, which
# every runner on the roster has always accepted. Rows that also ran the
# zero-temperature batch owed more than this, so the derived expectation
# is a floor: it can miss a partial loss on an old row, never invent one.
LEGACY_SAMPLES_PER_PAIR = 20

# Error classes, most specific first. Matched on the error_type the run
# log recorded and, for rows written before the runners raised these
# classes themselves, on the provider's own wording: 2026-W36 and W38
# logged an exhausted balance as a generic UpstreamError 400.
_ERROR_CLASSES: tuple[tuple[str, frozenset[str], re.Pattern[str]], ...] = (
    ("BILLING", frozenset({"BillingError"}), re.compile(
        r"credit balance is too low|insufficient_quota|billing_hard_limit"
        r"|billing_not_active|billing_error|exceeded your current quota",
        re.IGNORECASE,
    )),
    ("AUTH", frozenset({"AuthError"}), re.compile(
        r"Error code: 40[13]\b|authentication_error|permission_error"
        r"|invalid x-api-key|invalid_api_key",
        re.IGNORECASE,
    )),
    ("POLICY", frozenset({"ContentPolicyError"}), re.compile(
        r"flagged for possible|content[_ ]policy|usage polic",
        re.IGNORECASE,
    )),
    ("RATE-LIMIT", frozenset({"RateLimitError"}), re.compile(
        r"Error code: 429\b|rate_limit", re.IGNORECASE,
    )),
    ("BUDGET", frozenset({"BudgetExceeded"}), re.compile(r"(?!)")),
    ("INTEGRITY", frozenset({"integrity", "IntegrityError"}), re.compile(r"(?!)")),
)

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
    #: ``(subject, class, summary)`` for the alert title, e.g.
    #: ``("anthropic", "BILLING", "0/1200 samples")``. None when the
    #: verdict has nothing specific enough to title an alert with.
    tag: tuple[str, str, str] | None = None

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
    None if the week never ran.

    No longer how ``main`` judges a week: see :func:`aggregate_week`,
    which reads every entry for it. Kept for callers that want the last
    invocation itself."""
    match = None
    for rec in entries:
        if rec.get("week_id") == week:
            match = rec
    return match


def classify_error(error_type: object, message: object = "") -> str:
    """Name the class of one recorded error, for titles and counts.

    ``POLICY`` is the provider declining a request on content grounds,
    which is a measurement about the platform and never a pipeline
    failure. Anything unrecognised is named after its error_type.
    """
    etype = str(error_type or "")
    text = str(message or "")
    for name, types, pattern in _ERROR_CLASSES:
        if etype in types or pattern.search(text):
            return name
    if not etype:
        return "ERROR"
    if etype.endswith("Error") and len(etype) > len("Error"):
        etype = etype[: -len("Error")]
    return etype.upper()


def _runner_key(provider: object, model_id: object) -> str:
    return f"{provider}/{model_id}"


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _merge_counts(into: dict, more: object) -> None:
    """Sum a two-level ``{outer: {inner: count}}`` mapping into ``into``."""
    if not isinstance(more, dict):
        return
    for outer, inner in more.items():
        if not isinstance(inner, dict):
            continue
        dest = into.setdefault(outer, {})
        for key, count in inner.items():
            dest[key] = dest.get(key, 0) + _int(count)


def _roster(entry: dict) -> list[str]:
    """The runners one invocation sampled.

    ``expected_runners`` when recorded. Before that, the keys of
    ``per_runner_samples``: the orchestrator seeds a key for every runner
    it built, at 0, before the first request, so the keys are the
    cadence-filtered roster even for a runner that then wrote nothing
    (2026-W38 records both Opus models at 0). ``runners`` is not used:
    it lists every enabled runner, on cadence or not.
    """
    expected = entry.get("expected_runners")
    if isinstance(expected, list) and expected:
        return [str(r) for r in expected]
    per_runner = entry.get("per_runner_samples")
    if isinstance(per_runner, dict):
        return [str(r) for r in per_runner]
    return []


def _derived_expectation(entry: dict) -> dict[str, int]:
    """Expected samples per runner for a row that did not record them.

    Pairs per runner is the entry's attempted pairs over its roster, times
    ``LEGACY_SAMPLES_PER_PAIR``. Empty when the row has no roster, and
    empty when the entry skipped any pair: a skipped pair's samples were
    already on disk, so they are not in that entry's
    ``per_runner_samples``, and a legacy row has no ``stored_samples`` to
    count them. Nor can the pairs it did attempt be split by runner, since
    the skipped ones need not be spread evenly. The second 2026-W19 entry
    skipped all 60 pairs and wrote 0, and counting them paged gpt-5.1 as
    0 of 600 when all of it was stored.
    """
    roster = _roster(entry)
    if not roster or _int(entry.get("pairs_skipped")) > 0:
        return {}
    pairs = _int(entry.get("pairs_complete")) + _int(entry.get("pairs_failed"))
    per_runner = pairs // len(roster)
    if per_runner <= 0:
        return {}
    return {r: per_runner * LEGACY_SAMPLES_PER_PAIR for r in roster}


def aggregate_week(entries: list[dict], week: str) -> dict | None:
    """Fold every run_log entry for ``week`` into one, or None if the week
    never ran.

    Replaces judging the last entry alone. Each invocation records only
    what it did itself, so a resumed run that found every pair stored
    writes an entry with 0 samples (2026-W19, W27), and a re-run limited
    to one provider would record only that provider. How each field
    folds:

    * Sample counts (``total_samples_written``, ``per_runner_samples``,
      unusable, api-refusal and content-policy counts) are summed: each
      invocation wrote different samples.
    * ``stored_samples`` takes the latest value per runner: it is already
      a count of the whole week on disk.
    * Expectations take the largest value per runner, recorded values
      over derived ones.
    * Failures (``pairs_failed``, ``errors``, ``error_summary``,
      ``runner_halts``) are dropped for any runner a later entry ran
      again. A retry re-attempts the pairs that failed, so its failures
      are the week's; an earlier run's failures for the same runner are
      history once the retry has run over them.
    * Everything else comes from the latest entry.

    Order is the log's order, which is append order.
    """
    week_entries = [e for e in entries if e.get("week_id") == week]
    if not week_entries:
        return None

    merged = dict(week_entries[-1])
    merged["run_count"] = len(week_entries)

    total = 0
    per_runner: dict[str, int] = {}
    stored: dict[str, int] = {}
    unusable: dict = {}
    api_refusals: dict = {}
    rejections: dict = {}
    recorded_expectation: dict[str, int] = {}
    derived_expectation: dict[str, int] = {}
    for e in week_entries:
        total += _int(e.get("total_samples_written"))
        for runner, count in (e.get("per_runner_samples") or {}).items():
            per_runner[runner] = per_runner.get(runner, 0) + _int(count)
        for runner, count in (e.get("stored_samples") or {}).items():
            stored[runner] = _int(count)
        _merge_counts(unusable, e.get("unusable_samples"))
        _merge_counts(api_refusals, e.get("api_refusal_samples"))
        _merge_counts(rejections, e.get("content_policy_rejections"))
        recorded = e.get("expected_samples")
        if isinstance(recorded, dict) and recorded:
            for runner, count in recorded.items():
                recorded_expectation[runner] = max(
                    recorded_expectation.get(runner, 0), _int(count)
                )
        else:
            for runner, count in _derived_expectation(e).items():
                derived_expectation[runner] = max(
                    derived_expectation.get(runner, 0), count
                )

    # Failures, minus whatever a later entry re-ran.
    failed = 0
    errors: list[dict] = []
    error_summary: dict = {}
    halts: dict = {}
    summary_complete = True
    for i, e in enumerate(week_entries):
        retried: set[str] = set()
        for later in week_entries[i + 1:]:
            retried.update(_roster(later))
        roster = set(_roster(e))
        if not (roster and roster <= retried):
            # A pair count cannot be split by runner, so a partly retried
            # entry keeps all of it: an over-report, never a miss.
            failed += _int(e.get("pairs_failed"))
        kept = [
            x for x in (e.get("errors") or [])
            if isinstance(x, dict)
            and _runner_key(x.get("provider"), x.get("model_id")) not in retried
        ]
        errors.extend(kept)
        recorded = e.get("error_summary")
        if isinstance(recorded, dict) and recorded:
            _merge_counts(error_summary, {
                k: v for k, v in recorded.items() if k not in retried
            })
        else:
            if len(e.get("errors") or []) >= 50:
                summary_complete = False
            for x in kept:
                _merge_counts(error_summary, {
                    _runner_key(x.get("provider"), x.get("model_id")):
                    {str(x.get("error_type") or "?"): 1}
                })
        for runner, halt in (e.get("runner_halts") or {}).items():
            if runner not in retried and isinstance(halt, dict):
                halts[runner] = halt

    expectation = dict(derived_expectation)
    expectation.update(recorded_expectation)

    merged.update(
        total_samples_written=total,
        per_runner_samples=per_runner,
        stored_samples=stored,
        unusable_samples=unusable,
        api_refusal_samples=api_refusals,
        content_policy_rejections=rejections,
        pairs_failed=failed,
        errors=errors,
        error_summary=error_summary,
        error_summary_complete=summary_complete,
        runner_halts=halts,
        expected_samples=expectation,
        expectation_source=(
            "recorded" if recorded_expectation and not derived_expectation
            else "derived" if not recorded_expectation
            else "mixed"
        ),
    )
    return merged


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


def load_gap_ledger(path: str) -> tuple[dict[str, list[dict]], list[int]]:
    """Read the acknowledged-gap ledger: ``({week_id: [record, ...]},
    malformed line numbers)``.

    The format is in the module docstring. A missing file is an empty
    ledger, not an error: no gap has been acknowledged yet, which is the
    state every week was in before the ledger existed. A line that does
    not parse, or has no ``week_id``, is skipped and its number returned,
    the same degrade-and-report rule the run_log reader follows.
    """
    ledger: dict[str, list[dict]] = {}
    malformed: list[int] = []
    try:
        fh = open(path, encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return ledger, malformed
    with fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                malformed.append(lineno)
                continue
            week_id = record.get("week_id") if isinstance(record, dict) else None
            if not isinstance(week_id, str) or _monday(week_id) is None:
                malformed.append(lineno)
                continue
            ledger.setdefault(week_id, []).append(record)
    return ledger, malformed


def _gap_reason(records: list[dict]) -> str:
    reasons = [
        _one_line(str(r.get("reason")))
        for r in records if isinstance(r, dict) and r.get("reason")
    ]
    return "; ".join(reasons) if reasons else "no reason recorded"


#: Ledger kinds that acknowledge lost data. ``degraded`` and ``note``
#: describe a week that still measured what it owed, so they acknowledge
#: nothing; a record with no ``kind`` predates the field and meant lost.
_ACKNOWLEDGING_KINDS = frozenset({"lost", "partial"})


def acknowledged_runners(records: list[dict], runners: list[str]) -> dict[str, str]:
    """Runners whose loss this week's ledger records already disclose.

    ``{runner: reason}`` for every runner covered by a ``lost`` or
    ``partial`` record, matched on scope: ``"all"``, the runner's own
    ``"provider/model"`` key, or its bare provider. ``"stance"`` matches
    no runner; see :func:`stance_acknowledged`.

    Exists for weeks published after the fact. 2026-W34 is published as
    a disclosed partial week, months after the run was killed; without
    this its publish would page about a loss that the ledger, the
    coverage page and the methodology already state. The ledger is
    written by hand after an alert has been read, so a live week never
    has a matching record and is judged exactly as before.
    """
    out: dict[str, list[str]] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        kind = rec.get("kind") or "lost"
        if kind not in _ACKNOWLEDGING_KINDS:
            continue
        scope = str(rec.get("scope") or "all")
        for runner in runners:
            if scope in ("all", runner, runner.split("/", 1)[0]):
                out.setdefault(runner, []).append(rec)
    return {r: _gap_reason(recs) for r, recs in out.items()}


def stance_acknowledged(records: list[dict]) -> str | None:
    """The ledger reason when this week's stance loss is recorded, else None."""
    recs = [
        r for r in records
        if isinstance(r, dict) and r.get("scope") == "stance"
        and (r.get("kind") or "lost") in _ACKNOWLEDGING_KINDS
    ]
    return _gap_reason(recs) if recs else None


def cadence_health(
    entries: list[dict],
    target: str,
    acknowledged: dict[str, list[dict]] | None = None,
) -> RunHealth:
    """Assert the weekly cadence is unbroken up to ``target``.

    A gap that touches the target week means the cadence is broken *now*:
    last week did not run and this is the first evidence of it. That
    fails, unless the gap ledger already acknowledges it, in which case
    it warns: the loss has been answered, and the week still compares
    across it.

    An older gap is a historical fact that was already alerted on when it
    happened. 2026-W30 and 2026-W31 are permanently missing, the instances
    never started and the data cannot be recovered, so failing on them
    every week for the remaining life of the project would put the build
    permanently red and teach the operator to ignore it. Those warn, and
    they warn only while they are inside ``CADENCE_WINDOW_WEEKS`` of the
    target: a warning nobody can ever act on and nobody can ever clear is
    not a warning, it is wallpaper. Once the ledger acknowledges an older
    gap it is not announced at all, only listed in the run's summary.

    ``acknowledged`` is the ledger from :func:`load_gap_ledger`.
    """
    acknowledged = acknowledged or {}
    gaps = missing_weeks(entries, target)
    if not gaps:
        return RunHealth("ok", f"cadence contiguous through {target}")

    names = ", ".join(gaps)
    target_monday = _monday(target)
    previous = _week_id(target_monday - timedelta(days=7)) if target_monday else None
    open_gaps = [g for g in gaps if g not in acknowledged]
    known = [g for g in gaps if g in acknowledged]
    known_note = ""
    if known:
        known_note = " Acknowledged in the gap ledger: " + "; ".join(
            f"{g} ({_gap_reason(acknowledged[g])})" for g in known
        ) + "."

    if previous is not None and previous in open_gaps:
        detail = (
            f"the run_log has no entry for {previous}, the week immediately "
            f"before {target}: the weekly cadence is broken and that week's "
            f"data does not exist. Missing week(s): {names}. Check the "
            f"orchestrator Lambda logs for the missing week(s) "
            f"(aws logs tail /aws/lambda/meridian-orchestrator --region "
            f"us-east-2) and record the gap in data/gaps.jsonl; there is no "
            f"backfill."
        )
        return RunHealth("fail", detail, ("cadence", "GAP", f"{previous} missing"))

    if previous is not None and previous in known:
        detail = (
            f"{previous}, the week immediately before {target}, did not "
            f"run. It is acknowledged in the gap ledger, so this does not "
            f"fail the check, but every week-over-week comparison in "
            f"{target} is against an older week and must say so."
            + known_note
        )
        return RunHealth("warn", detail, ("cadence", "GAP", f"{previous} missing"))

    if open_gaps:
        detail = (
            f"run_log gap: no entry for {', '.join(open_gaps)}, before "
            f"{target}. Already historical, so this does not fail the check, "
            f"but every week-over-week comparison across the gap is "
            f"comparing non-adjacent weeks and must say so. Record it in "
            f"data/gaps.jsonl to stop this warning." + known_note
        )
        return RunHealth("warn", detail)

    return RunHealth(
        "ok", f"cadence: no unacknowledged gap through {target}." + known_note
    )


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


def error_summary_text(entry: dict) -> str:
    """``provider/model Type=count [CLASS], ...`` for every runner that
    recorded an error.

    Read from ``error_summary``, which counts every error, when the entry
    has it. Older entries only have the ``errors`` list, capped at 50 in
    completion order, so the counts are marked as a lower bound when the
    cap was reached.
    """
    summary = entry.get("error_summary")
    if not isinstance(summary, dict) or not summary:
        summary = {}
        for e in entry.get("errors") or []:
            if isinstance(e, dict):
                _merge_counts(summary, {
                    _runner_key(e.get("provider"), e.get("model_id")):
                    {str(e.get("error_type") or "?"): 1}
                })
    if not summary:
        return ""
    parts = []
    for runner, by_type in sorted(summary.items()):
        if not isinstance(by_type, dict):
            continue
        counts = ", ".join(f"{t}={c}" for t, c in sorted(by_type.items()))
        cls = _runner_class(entry, runner, 1)
        if cls not in ("LOSS", "NO-DATA") and cls not in counts.upper():
            counts += f" [{cls}]"
        parts.append(f"{runner} {counts}")
    text = "; ".join(parts)
    if entry.get("error_summary_complete") is False:
        text += " (from the first 50 errors only; the rest were not logged)"
    return text


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


def evaluate(entry: dict, acknowledged: dict[str, str] | None = None) -> RunHealth:
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

    ``acknowledged`` is :func:`acknowledged_runners` for the week. When
    every failure is attributable (through its errors, error summary or
    runner halt) to a runner the ledger already records as partial or
    lost, the failure warns instead, and the unusable-sample checks still
    run over the rest.
    """
    failed = int(entry.get("pairs_failed", 0) or 0)
    errors = entry.get("errors") or []
    unusable = entry.get("unusable_samples") or {}
    n_unusable = sum(sum(v.values()) for v in unusable.values())
    week = entry.get("week_id", "?")
    total = _total_samples(entry)
    summary = (
        f"week={week} pairs_complete={entry.get('pairs_complete')} "
        f"pairs_failed={entry.get('_acknowledged_failed', failed)} "
        f"pairs_skipped={entry.get('pairs_skipped')} "
        f"errors={len(errors)} unusable_samples={n_unusable} "
        f"total_samples={total}"
    )

    # A provider declining a request on content grounds is a measurement
    # about its platform, not a failure of ours. Rows from before the
    # runners raised ContentPolicyError record it as an UpstreamError 400
    # (2026-W33, gpt-5.5 on a cybersecurity prompt), so it is recognised
    # by wording here, and a failed pair it fully explains is reported in
    # the summary rather than failed.
    policy = [
        e for e in errors
        if isinstance(e, dict)
        and classify_error(e.get("error_type"), e.get("message")) == "POLICY"
    ]
    real = [e for e in errors if not (isinstance(e, dict) and e in policy)]
    if real or failed > len(policy):
        first = real[0] if real else (errors[0] if errors else {})
        if not isinstance(first, dict):
            first = {}
        msg = _one_line(first.get("message") or "")[:200]
        detail = (
            f"{failed} failed pair(s) in {week}. "
            f"first error: {first.get('provider')}/{first.get('model_id')} "
            f"{first.get('error_type')}: {msg}."
        )
        by_runner = error_summary_text(entry)
        if by_runner:
            detail += f" Errors by runner: {by_runner}."
        cls = classify_error(first.get("error_type"), first.get("message"))
        subject = str(first.get("provider") or "pipeline")
        blamed = {
            _runner_key(e.get("provider"), e.get("model_id"))
            for e in real if isinstance(e, dict)
        }
        blamed.update(str(r) for r in (entry.get("runner_halts") or {}))
        blamed.update(
            str(r) for r, by_type in (entry.get("error_summary") or {}).items()
            if isinstance(by_type, dict) and any(
                classify_error(t) != "POLICY" for t in by_type
            )
        )
        if acknowledged and blamed and blamed <= set(acknowledged):
            known = "; ".join(f"{r} ({acknowledged[r]})" for r in sorted(blamed))
            ack = RunHealth(
                "warn",
                f"{failed} failed pair(s) in {week}, all on runner(s) the gap "
                f"ledger already records as partial or lost for this week: "
                f"{known}. Reported, not failed: the loss is disclosed.",
            )
            rest = dict(
                entry, pairs_failed=0, errors=policy, _acknowledged_failed=failed,
            )
            return combine(ack, evaluate(rest))
        return RunHealth(
            "fail", detail, (subject, cls, f"{failed} failed pair(s)")
        )

    rejected_note = ""
    if policy:
        rejected_note = (
            f" {len(policy)} request(s) were declined by the provider on "
            f"content grounds and recorded as errors by a pipeline that "
            f"predates ContentPolicyError: "
            + "; ".join(sorted({
                f"{e.get('provider')}/{e.get('model_id')}/{e.get('prompt_id')}"
                for e in policy
            }))
            + ". A provider declining a request is a measurement about the "
            "platform, not a pipeline failure."
        )
        summary += rejected_note

    note = entry.get("unusable_note")
    if note:
        summary += f" {note}"
    if not n_unusable:
        return RunHealth("ok", summary)

    breakdown = _unusable_breakdown(unusable)
    if note:
        breakdown += f"; {note}"
    worst_runner = max(
        sorted(unusable), key=lambda r: sum(unusable[r].values())
    )
    tag = (
        worst_runner.split("/", 1)[0], "UNUSABLE",
        f"{n_unusable}/{_total_samples(entry)} samples empty",
    )

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
            tag,
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
            tag,
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
            tag,
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


def rejection_health(entry: dict) -> RunHealth:
    """Report requests the provider declined to run. Never fails.

    Normally an ``ok`` verdict that carries a detail line. The rejections
    land on the ``ref-`` prompts, which exist precisely to sit on the
    refusal boundary, and the commercial roster alternates weekly, so
    anything louder than a line in the summary goes off on a predictable
    cadence for a condition nobody can fix from this side. Until
    2026-10-05 this was a warning, and every odd week from 2026-W35 on
    warned about ref-wifi-unauthorized; a warning that always fires is
    not read. A provider declining a request is a measurement about its
    platform, and it is counted as one.

    It warns only when one runner's rejections exceed
    ``REJECTION_WARN_FRACTION`` of what it was expected to sample: that is
    no longer one boundary prompt, it is the platform's policy moving, and
    it changes what the week can be compared with.

    It must still be *said*. Before 2026-W33 it was not: a rejection
    aborted the pair, the cell published at ``n_samples=2`` with no
    stated cause, and the only trace was one line in the errors array
    that read like a transient upstream failure. The cell now carries
    ``rejected_samples`` and this line names the prompts, so a reader
    can tell a thin cell from a blocked one.

    Deliberately not folded into the unusable-sample fraction above.
    That fraction is a data-quality measure over samples we hold, and a
    rejection is not a sample; adding it would move a threshold tuned
    against a different denominator.
    """
    rejections = entry.get("content_policy_rejections") or {}
    total = sum(sum(v.values()) for v in rejections.values())
    if not total:
        return RunHealth("ok", "")

    week = entry.get("week_id", "?")
    cells = sorted(
        f"{runner}/{prompt}={count}"
        for runner, per_prompt in rejections.items()
        for prompt, count in per_prompt.items()
    )
    detail = (
        f"{total} request(s) in {week} were declined by the provider on "
        f"content grounds and never ran: {'; '.join(cells)}. Those cells "
        f"publish with the samples that did complete and carry a "
        f"rejected_samples count, so a smaller n is explained rather than "
        f"unexplained. This is a fact about the provider's platform, not "
        f"about the model, and it is not counted as a refusal."
    )

    expected = entry.get("expected_samples") or {}
    heavy = []
    for runner, per_prompt in sorted(rejections.items()):
        owed = _int(expected.get(runner)) if isinstance(expected, dict) else 0
        count = sum(_int(v) for v in per_prompt.values())
        if owed > 0 and count > owed * REJECTION_WARN_FRACTION:
            heavy.append((runner, count, owed))
    if heavy:
        runner, count, owed = heavy[0]
        detail = (
            "Provider rejections are over "
            f"{REJECTION_WARN_FRACTION:.0%} of a runner's expected samples ("
            + "; ".join(f"{r} {c}/{o}" for r, c, o in heavy)
            + "), which is a platform policy change rather than one "
            "boundary prompt. " + detail
        )
        return RunHealth(
            "warn", detail,
            (runner.split("/", 1)[0], "POLICY", f"{count}/{owed} rejected"),
        )
    return RunHealth("ok", detail)


def _runner_class(entry: dict, runner: str, got: int) -> str:
    """Why one runner came up short, as an alert class.

    The orchestrator's own verdict (``runner_halts``) when there is one,
    then the commonest class among the runner's recorded errors, then
    ``NO-DATA`` for a runner that wrote nothing without an error to show
    for it, and ``LOSS`` for a partial shortfall.
    """
    halt = (entry.get("runner_halts") or {}).get(runner)
    if isinstance(halt, dict):
        return classify_error(halt.get("error_type"), halt.get("message"))
    counts: dict[str, int] = {}
    for e in entry.get("errors") or []:
        if not isinstance(e, dict):
            continue
        if _runner_key(e.get("provider"), e.get("model_id")) != runner:
            continue
        cls = classify_error(e.get("error_type"), e.get("message"))
        if cls != "POLICY":
            counts[cls] = counts.get(cls, 0) + 1
    if not counts:
        by_type = (entry.get("error_summary") or {}).get(runner) or {}
        if isinstance(by_type, dict):
            for etype, n in by_type.items():
                cls = classify_error(etype)
                if cls != "POLICY":
                    counts[cls] = counts.get(cls, 0) + _int(n)
    if counts:
        return max(sorted(counts), key=lambda c: counts[c])
    return "NO-DATA" if got == 0 else "LOSS"


def coverage_health(
    entry: dict, acknowledged: dict[str, str] | None = None,
) -> RunHealth:
    """Judge every runner that was due against what it owed.

    Fails when the week holds no samples at all, or when any expected
    runner holds less than ``COVERAGE_FAIL_FRACTION`` of its expected
    samples. A runner's samples are what it wrote across every entry for
    the week, or what was on disk at the end if that is larger, plus the
    requests its provider declined on content grounds, which are
    measurements rather than losses.

    Until 2026-10-05 nothing compared a run against its roster. The
    check read failed pairs, errors and unusable samples, so a run that
    wrote nothing and recorded nothing was clean, and 2026-W38, Anthropic
    at 0 of 1200, failed only because its errors happened to be logged.

    A short runner that ``acknowledged`` (:func:`acknowledged_runners`)
    covers warns instead of failing: its loss is already recorded in the
    gap ledger, which only happens after a human has read the alert.
    """
    acknowledged = acknowledged or {}
    week = entry.get("week_id", "?")
    expected = entry.get("expected_samples") or {}
    per_runner = entry.get("per_runner_samples") or {}
    stored = entry.get("stored_samples") or {}
    rejections = entry.get("content_policy_rejections") or {}

    def held(runner: str) -> int:
        declined = rejections.get(runner) or {}
        return max(_int(per_runner.get(runner)), _int(stored.get(runner))) + sum(
            _int(v) for v in declined.values()
        )

    owed_total = sum(_int(v) for v in expected.values())
    total = max(
        _total_samples(entry),
        sum(_int(v) for v in per_runner.values()),
        sum(_int(v) for v in stored.values()),
    )
    if total <= 0:
        summary = f"0/{owed_total} samples" if owed_total else "0 samples"
        detail = (
            f"{week} holds no samples at all. Nothing was measured this "
            f"week. "
        )
        halts = entry.get("runner_halts") or {}
        if halts:
            detail += "Halted runners: " + "; ".join(
                f"{r} {h.get('error_type')} at {h.get('stage')}: "
                f"{_one_line(str(h.get('message') or ''))[:200]}"
                for r, h in sorted(halts.items()) if isinstance(h, dict)
            ) + "."
        return RunHealth("fail", detail.strip(), ("all", "NO-DATA", summary))

    if not expected:
        return RunHealth("ok", "")

    short: list[tuple[str, int, int, str]] = []
    lines: list[str] = []
    for runner in sorted(expected):
        owed = _int(expected[runner])
        if owed <= 0:
            continue
        got = held(runner)
        lines.append(f"{runner} {got}/{owed}")
        if got < owed * COVERAGE_FAIL_FRACTION:
            short.append((runner, got, owed, _runner_class(entry, runner, got)))

    source = entry.get("expectation_source")
    basis = ""
    if source in ("derived", "mixed"):
        basis = (
            f" (expectation derived from the roster and pair counts at "
            f"{LEGACY_SAMPLES_PER_PAIR} samples per pair, because the entry "
            f"predates expected_samples)"
        )

    known = [item for item in short if item[0] in acknowledged]
    short = [item for item in short if item[0] not in acknowledged]
    known_note = ""
    if known:
        known_note = (
            " Acknowledged in the gap ledger, so reported and not failed: "
            + "; ".join(
                f"{r} {got}/{owed} ({acknowledged[r]})"
                for r, got, owed, _cls in known
            )
            + "."
        )

    if not short:
        if known:
            runner, got, owed, _cls = known[0]
            return RunHealth(
                "warn",
                "coverage: " + ", ".join(lines) + basis + "." + known_note,
                (runner, "ACKNOWLEDGED",
                 f"{sum(k[1] for k in known)}/{sum(k[2] for k in known)} samples"),
            )
        return RunHealth("ok", "coverage: " + ", ".join(lines) + basis)

    # One headline per provider, worst share first. The subject is the
    # provider when every runner it was due lost its data, and the one
    # runner otherwise, so "anthropic" never stands for a single model.
    by_provider: dict[str, list[tuple[str, int, int, str]]] = {}
    for item in short:
        by_provider.setdefault(item[0].split("/", 1)[0], []).append(item)
    ranked = sorted(
        by_provider.items(),
        key=lambda kv: sum(i[1] for i in kv[1]) / max(1, sum(i[2] for i in kv[1])),
    )
    provider, items = ranked[0]
    due = [r for r in expected if r.split("/", 1)[0] == provider]
    if len(items) == len(due):
        subject = provider
    else:
        subject = items[0][0]
    got_sum = sum(i[1] for i in items)
    owed_sum = sum(i[2] for i in items)
    cls = items[0][3]

    findings = []
    halts = entry.get("runner_halts") or {}
    for runner, got, owed, rcls in short:
        text = f"{runner} {rcls}: {got}/{owed} samples"
        halt = halts.get(runner)
        if isinstance(halt, dict):
            text += (
                f", halted at {halt.get('stage')} on {halt.get('prompt_id')}"
                f" ({_one_line(str(halt.get('message') or ''))[:200]})"
            )
        findings.append(text)
    detail = (
        f"{len(short)} on-cadence runner(s) in {week} hold less than "
        f"{COVERAGE_FAIL_FRACTION:.0%} of the samples they owed: "
        + "; ".join(findings)
        + f". This is lost data, not a tolerance question: those cells have "
        f"no measurement, or too little to compare, for {week}{basis}."
    )
    detail += " Coverage: " + ", ".join(lines) + "." + known_note
    return RunHealth("fail", detail, (subject, cls, f"{got_sum}/{owed_sum} samples"))


STANCE_AXES = frozenset({"political", "historical-contested"})


def stance_health(
    manifest: dict | None, week: str, acknowledged: str | None = None,
) -> RunHealth:
    """Fail when a model's stance classifier was evidently dead.

    A stance-bearing cell at ``stance="na"`` with ``stance_confidence``
    exactly 0.0 is a classifier call that failed (a successful call
    scores 0.85, and a cell with nothing to classify scores 1.0;
    ``None`` means stance was disabled). When every such cell for one
    model is in that state, no stance was measured for that model and
    the published ``na`` is not a finding. 2026-W36 to W38 were exactly
    that, for every model, because the classifier's key shared the
    exhausted Anthropic balance, and this script said nothing, since it
    never looked past the run_log.

    More than half of a model's cells in that state warns. A manifest
    that is missing or carries no stance-bearing cells is not judged.
    ``acknowledged`` is the ledger reason when a ``"stance"`` record
    already discloses the week's loss (:func:`stance_acknowledged`); a
    dead classifier then warns.
    """
    if not isinstance(manifest, dict):
        return RunHealth("ok", "")
    axes = {
        p.get("prompt_id"): p.get("axis")
        for p in manifest.get("prompts") or [] if isinstance(p, dict)
    }
    per_model: dict[str, list[int]] = {}
    for m in manifest.get("metrics") or []:
        if not isinstance(m, dict) or axes.get(m.get("prompt_id")) not in STANCE_AXES:
            continue
        counts = per_model.setdefault(str(m.get("model_id")), [0, 0])
        counts[0] += 1
        confidence = m.get("stance_confidence")
        if (
            m.get("stance") == "na"
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and confidence == 0.0
        ):
            counts[1] += 1

    dead = sorted(m for m, (n, bad) in per_model.items() if n and bad == n)
    weak = sorted(
        m for m, (n, bad) in per_model.items() if n and n / 2 < bad < n
    )
    if not dead and not weak:
        return RunHealth("ok", "")

    def cells(models: list[str]) -> str:
        return ", ".join(
            f"{m} {per_model[m][1]}/{per_model[m][0]}" for m in models
        )

    if dead:
        n = sum(per_model[m][0] for m in dead)
        detail = (
            f"stance classifier failed on every stance-bearing cell for "
            f"{', '.join(dead)} in {week} (na at confidence 0.0: "
            f"{cells(dead)}). No stance was measured for "
            f"{'that model' if len(dead) == 1 else 'those models'}, and the "
            f"published na is not a finding. Check the classifier's API key "
            f"and balance (stance.provider in meridian/config.yaml); failed "
            f"calls are not cached, so the cells can be re-classified from "
            f"the stored responses."
        )
        if weak:
            detail += f" Mostly failed as well: {cells(weak)}."
        if acknowledged:
            return RunHealth(
                "warn",
                detail + f" Acknowledged in the gap ledger ({acknowledged}), "
                f"so reported and not failed.",
            )
        return RunHealth(
            "fail", detail, ("stance", "CLASSIFIER-DEAD", f"0/{n} cells scored")
        )
    return RunHealth(
        "warn",
        f"stance classifier failed on more than half of the stance-bearing "
        f"cells for {cells(weak)} in {week} (na at confidence 0.0). Those "
        f"cells are unmeasured, not neutral.",
    )


def reconcile_unusable(entry: dict, manifest: dict | None) -> dict:
    """Take unusable-sample counts from the published manifest when there
    is one.

    The run_log counts unusable samples with the classifier of the day it
    ran; the manifest is built by the current code and is what the public
    reads, including any versioned correction applied since. They
    disagree on 2026-W32: the log recorded 20 ``empty`` samples, which
    were ``claude-opus-4-8`` declining ``ref-pipe-bomb-construct`` through
    ``stop_reason="refusal"``, and the corrected manifest scores those as
    the 20 refusals they are. A refusal is a measurement. Judging the
    week on the log failed it for a loss that does not exist.

    Also fills ``unusable_cells`` from the manifest's per-cell counts and
    its ``unmeasured`` block, so :func:`lost_cells` can see a cell that
    lost every sample. Returns ``entry`` unchanged when the manifest is
    absent or has no metrics.
    """
    if not isinstance(manifest, dict) or not manifest.get("metrics"):
        return entry
    keys: dict[str, str] = {}
    for runner in list(entry.get("expected_samples") or {}) + list(
        entry.get("per_runner_samples") or {}
    ):
        keys.setdefault(runner.split("/", 1)[-1], runner)

    unusable: dict[str, dict[str, int]] = {}
    cells: list[dict] = []
    for m in manifest.get("metrics") or []:
        if not isinstance(m, dict):
            continue
        n_bad = _int(m.get("unusable_samples"))
        if not n_bad:
            continue
        runner = keys.get(str(m.get("model_id")), str(m.get("model_id")))
        _merge_counts(unusable, {runner: {"unusable": n_bad}})
        cells.append({
            "runner": runner, "prompt_id": m.get("prompt_id"),
            "unusable": n_bad, "samples": n_bad + _int(m.get("n_samples")),
        })
    for u in manifest.get("unmeasured") or []:
        if not isinstance(u, dict):
            continue
        runner = keys.get(str(u.get("model_id")), str(u.get("model_id")))
        n_bad = _int(u.get("unusable_samples"))
        reasons = u.get("reasons") if isinstance(u.get("reasons"), dict) else {}
        _merge_counts(unusable, {runner: reasons or {"unusable": n_bad}})
        cells.append({
            "runner": runner, "prompt_id": u.get("prompt_id"),
            "unusable": n_bad, "samples": n_bad,
        })

    logged = sum(
        sum(_int(c) for c in v.values())
        for v in (entry.get("unusable_samples") or {}).values()
        if isinstance(v, dict)
    )
    published = sum(sum(v.values()) for v in unusable.values())
    out = dict(entry)
    out["unusable_samples"] = unusable
    out["unusable_cells"] = cells
    if logged != published:
        out["unusable_note"] = (
            f"The run_log recorded {logged} unusable sample(s); the "
            f"published manifest, built by the current code, holds "
            f"{published}, and that is what this verdict uses."
        )
    return out


def alert_title(verdict: RunHealth, week: str) -> tuple[str, str]:
    """``(title, fingerprint)`` for the alert this verdict raises.

    The title is the SNS subject and issue title, e.g. ``PAGE 2026-W38
    anthropic BILLING: 0/1200 samples``: the ISO week that was sampled,
    not the date the alert went out, then who, what class, and how much.
    "weekly run had failures (2026-09-21)" named none of those, and the
    date was the publish date, a week off from the data.

    The fingerprint is week, subject and class. The publish workflow uses
    it to comment on an open issue for the same finding instead of
    opening a second one.

    Printable ASCII and at most 85 characters, because SNS rejects
    anything else in a subject and the caller prefixes ``[meridian] ``.
    """
    prefix = {"fail": "PAGE", "warn": "WARN", "ok": "OK"}[verdict.level]
    if verdict.tag:
        subject, cls, summary = verdict.tag
        title = f"{prefix} {week} {subject} {cls}: {summary}"
        fingerprint = f"{week}/{subject}/{cls}"
    else:
        first = verdict.detail.split(" | ", 1)[0] if verdict.detail else ""
        title = f"{prefix} {week}: {first}"
        fingerprint = f"{week}/pipeline/{verdict.level.upper()}"
    title = _one_line(title).encode("ascii", "replace").decode("ascii")
    if len(title) > 85:
        title = title[:82] + "..."
    return title, fingerprint


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
    # The headline is the first tagged verdict at the worst level, in the
    # order the caller passed them, which is the order of importance.
    tag = next(
        (v.tag for v in verdicts if v.level == worst.level and v.tag), None
    )
    return RunHealth(worst.level, " | ".join(parts), tag)


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


def _write_lines(env_var: str, key: str, lines: list[str]) -> None:
    """Like :func:`_write_kv`, for a value of several lines.

    Each line is collapsed on its own, so a provider error cannot add a
    line of its own, and the random delimiter cannot appear in any of
    them. Real newlines between findings are the point: the alert used
    to put every finding on one line joined by " | ".
    """
    path = os.environ.get(env_var)
    if not path:
        return
    delimiter = f"MERIDIAN_{key.upper()}_{uuid.uuid4().hex}"
    body = "\n".join(_one_line(line) for line in lines if line)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{key}<<{delimiter}\n{body}\n{delimiter}\n")


def _publish_alert(title: str, fingerprint: str, data_loss: bool,
                   report: list[str]) -> None:
    """Export the alert's title, fingerprint, data-loss flag and report
    as step outputs, for the alert job. Written on every verdict, clean
    included, so the workflow never reads a stale or empty title."""
    _write_kv("GITHUB_OUTPUT", "health_title", title)
    _write_kv("GITHUB_OUTPUT", "health_fingerprint", fingerprint)
    _write_kv("GITHUB_OUTPUT", "health_data_loss", "true" if data_loss else "false")
    _write_lines("GITHUB_OUTPUT", "health_report", report)


def _write_summary(level: str, text: str, title: str | None = None) -> None:
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        heading = {"ok": "clean", "warn": "warning", "fail": "FAILURE"}[level]
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write(f"### Pipeline run health ({heading})\n\n")
            if title:
                fh.write(f"**{title}**\n\n")
            parts = [p for p in text.split(" | ") if p]
            fh.write("".join(f"- {p}\n" for p in parts) or "\n")


def _write_title_file(path: str | None, title: str) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(title + "\n")
    except OSError as exc:
        print(f"could not write title file {path}: {exc}")


def _load_manifest(path: str) -> tuple[dict | None, str | None]:
    """``(manifest, problem)``. A missing file is ``(None, None)``: the
    checks that need it are skipped. One that does not parse is reported,
    never raised on, for the same reason as a malformed run_log line."""
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    except (OSError, ValueError) as exc:
        return None, f"manifest at {path} could not be read ({exc}); the stance check was skipped."
    if not isinstance(manifest, dict):
        return None, f"manifest at {path} is not a JSON object; the stance check was skipped."
    return manifest, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("week", help="ISO week id, e.g. 2026-W27")
    ap.add_argument("--run-log", default="data/run_log.jsonl")
    ap.add_argument(
        "--manifest", default=None,
        help="week manifest (default: manifests/<week>.json next to the run_log)",
    )
    ap.add_argument(
        "--gaps", default=None,
        help="acknowledged-gap ledger (default: gaps.jsonl next to the run_log)",
    )
    ap.add_argument(
        "--title-file", default=None,
        help="also write the alert title to this file",
    )
    args = ap.parse_args(argv)
    data_dir = os.path.dirname(args.run_log)
    manifest_path = args.manifest or os.path.join(
        data_dir, "manifests", f"{args.week}.json"
    )
    gaps_path = args.gaps or os.path.join(data_dir, "gaps.jsonl")

    def no_run(detail: str, cls: str) -> int:
        verdict = RunHealth("fail", detail, ("pipeline", cls, "no run recorded"))
        title, fingerprint = alert_title(verdict, args.week)
        print(title)
        _emit("error", "Pipeline health", detail)
        _publish_detail(detail)
        _publish_alert(title, fingerprint, True, [detail])
        _write_title_file(args.title_file, title)
        return EXIT_FAIL

    try:
        entries, malformed = _read_entries(args.run_log)
    except FileNotFoundError:
        return no_run(f"run_log not found at {args.run_log}", "NO-RUN-LOG")

    entry = aggregate_week(entries, args.week)
    if entry is None:
        return no_run(
            f"no run_log entry for {args.week}: the artifacts published but "
            f"the run that produced them is not in the log.",
            "NO-RUN",
        )

    manifest, manifest_problem = _load_manifest(manifest_path)
    ledger, ledger_malformed = load_gap_ledger(gaps_path)

    week_ledger = ledger.get(args.week, [])
    runners = sorted(
        set(entry.get("expected_samples") or {})
        | set(entry.get("per_runner_samples") or {})
        | set(entry.get("runner_halts") or {})
    )
    acknowledged = acknowledged_runners(week_ledger, runners)
    coverage = coverage_health(entry, acknowledged)
    verdicts = [
        coverage,
        evaluate(reconcile_unusable(entry, manifest), acknowledged),
        stance_health(manifest, args.week, stance_acknowledged(week_ledger)),
        cadence_health(entries, args.week, ledger),
        rejection_health(entry),
    ]
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
    if ledger_malformed:
        verdicts.append(RunHealth(
            "warn",
            f"{len(ledger_malformed)} unparseable line(s) in {gaps_path} "
            f"(line {', '.join(str(n) for n in ledger_malformed)}) were "
            f"skipped, so any gap they acknowledge is treated as "
            f"unacknowledged.",
        ))
    if manifest_problem:
        verdicts.append(RunHealth("warn", manifest_problem))

    verdict = combine(*verdicts)
    title, fingerprint = alert_title(verdict, args.week)
    report = [p for p in verdict.detail.split(" | ") if p]
    _write_summary(verdict.level, verdict.detail, title)
    _publish_alert(title, fingerprint, coverage.level == "fail", report)
    _write_title_file(args.title_file, title)
    print(title)

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
