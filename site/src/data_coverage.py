"""Per-(week, model) coverage ledger for the public site.

Built for the 2026-10 disclosure pass. Until then the public record
stated its gaps in hand-written prose on /methodology/, and that prose
had drifted from the data in both directions: 2026-W34 was described as
unpublished while its metrics were served under /data/2026-W34/, and
2026-W36 to W38 (Anthropic credit exhausted, stance classifier down for
every model) were not described at all. A reader had no way to tell an
on-cadence model that lost its week from an off-cadence model that was
never due.

This module derives one row per (week, model) from three inputs, so the
statement of what the record holds is computed from the record:

* the manifest the site is built from (its history plus the current
  week), which says what was published;
* ``data/run_log.jsonl``, which says what each run was due to sample
  and what it wrote;
* ``data/gaps.jsonl``, the hand-maintained gap ledger, which says why.

The ledger format is defined by ``scripts/check_run_health.py`` (its
module docstring), which reads ``week_id``, ``reason``, ``scope`` and
``recorded_at``. This module reads the same lines with the same
tolerance (a line that does not parse, or has no ISO ``week_id``, is
skipped and counted, never fatal) and additionally reads:

``kind``
    ``lost`` (nothing captured), ``partial`` (some prompts or samples
    missing), ``degraded`` (captured but known to be impaired) or
    ``note`` (context that changes no status), or ``corrected`` (a later
    correction of something an earlier record disclosed; it changes no
    status and is shown next to the records it answers, see below). A
    record with no kind is treated as ``lost``, which is what the health
    check has always assumed a ledger line means. A ``corrected`` record
    answers every earlier ``lost``/``partial``/``degraded`` record of the
    same week and scope: those stay on the page (the ledger is
    append-only and the original disclosure is part of the record) but
    are shown as corrected and no longer count as an open warning.
``scope``
    ``"all"``, a ``"provider/model"`` key, a bare provider such as
    ``"anthropic"`` (every model of that provider in the week), or
    ``"stance"`` (the stance classifier, across models).
``runners``
    For a week with no run log entry at all, the ``provider/model`` keys
    that were due. Without it a wholly lost week has no roster to show.
``evidence``
    A list of pointers: ``"issue #36"``, ``"commit de2ec54"``, a
    site-root path such as ``"/reports/<slug>/"``, or free text.

Nothing here writes to ``data/``. The ledger and the run log are read
only, and only through paths the caller passes in, so tests point them
at ``tmp_path`` files.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from schema import Manifest

REPO_URL = "https://github.com/digital-grease/meridian"

#: Statuses, most to least severe. ``not-scheduled`` is the ordinary
#: state of an alternating frontier model in its off week.
STATUSES = ("lost", "partial", "degraded", "scheduled", "ok", "not-scheduled")

STATUS_LABELS = {
    "ok": "OK",
    "partial": "Partial",
    "degraded": "Degraded",
    "lost": "Lost",
    "scheduled": "Not yet published",
    "not-scheduled": "Not scheduled",
}

#: Ledger kinds that set a cell's status. ``note`` and ``corrected`` set none.
_STATUS_KINDS = ("lost", "partial", "degraded")
_INFO_KINDS = ("note", "corrected")

_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
_ISSUE_RE = re.compile(r"^issue #(\d+)$", re.IGNORECASE)
_COMMIT_RE = re.compile(r"^commit ([0-9a-f]{7,40})$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path | None) -> tuple[list[tuple[int, dict]], list[int]]:
    """Every JSON object line in ``path`` with its line number, plus
    malformed line numbers.

    A missing file is an empty list, not an error: the ledger did not
    exist before 2026-10, and a fresh checkout has no run log.
    """
    rows: list[tuple[int, dict]] = []
    bad: list[int] = []
    if path is None:
        return rows, bad
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return rows, bad
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            bad.append(lineno)
            continue
        if not isinstance(obj, dict):
            bad.append(lineno)
            continue
        rows.append((lineno, obj))
    return rows, bad


def load_gap_ledger(path: Path | None) -> tuple[list[dict], list[int]]:
    """The gap ledger's records in file order, plus skipped line numbers.

    Same acceptance rule as ``check_run_health.load_gap_ledger``: a
    record needs a ``week_id`` that looks like an ISO week. Everything
    else is optional and read defensively.
    """
    rows, bad = _read_jsonl(path)
    out: list[dict] = []
    for lineno, row in rows:
        week = row.get("week_id")
        if not isinstance(week, str) or not _WEEK_RE.match(week):
            bad.append(lineno)
            continue
        out.append(row)
    return out, sorted(bad)


def load_run_log_rows(path: Path | None) -> list[dict]:
    """Raw run log rows. Read as plain JSON rather than through
    ``RunLogEntry`` so a row carrying a field this site build does not
    know yet (a newer pipeline's addition) still renders."""
    rows, _ = _read_jsonl(path)
    return [r for _, r in rows if isinstance(r.get("week_id"), str)]


# ---------------------------------------------------------------------------
# Evidence and ledger records
# ---------------------------------------------------------------------------


def evidence_links(evidence: object) -> list[dict]:
    """``[{"text", "href"}]`` for a record's ``evidence``.

    Accepts a list or a single string (split on ``;``). ``href`` is None
    for a pointer with no public URL, such as a run log timestamp.
    """
    if isinstance(evidence, str):
        items = [e.strip() for e in evidence.split(";")]
    elif isinstance(evidence, list):
        items = [str(e).strip() for e in evidence]
    else:
        items = []
    out: list[dict] = []
    for item in items:
        if not item:
            continue
        href: str | None = None
        m = _ISSUE_RE.match(item)
        if m:
            href = f"{REPO_URL}/issues/{m.group(1)}"
        else:
            m = _COMMIT_RE.match(item)
            if m:
                href = f"{REPO_URL}/commit/{m.group(1)}"
            elif item.startswith("/") and " " not in item:
                href = item
        out.append({"text": item, "href": href})
    return out


@dataclass(frozen=True)
class LedgerRecord:
    week_id: str
    scope: str
    kind: str
    reason: str
    evidence: list[dict]
    recorded_at: str | None
    runners: list[str]

    @property
    def scope_label(self) -> str:
        if self.scope == "all":
            return "all runners"
        if self.scope == "stance":
            return "stance classifier"
        return self.scope.split("/", 1)[-1] if "/" in self.scope else self.scope


def ledger_records(raw: Iterable[dict]) -> list[LedgerRecord]:
    out: list[LedgerRecord] = []
    for r in raw:
        kind = r.get("kind")
        kind = kind if kind in (*_STATUS_KINDS, *_INFO_KINDS) else "lost"
        scope = r.get("scope")
        scope = scope if isinstance(scope, str) and scope else "all"
        runners = r.get("runners")
        out.append(LedgerRecord(
            week_id=r["week_id"],
            scope=scope,
            kind=kind,
            reason=str(r.get("reason") or "No reason recorded."),
            evidence=evidence_links(r.get("evidence")),
            recorded_at=(str(r["recorded_at"]) if r.get("recorded_at") else None),
            runners=[str(x) for x in runners] if isinstance(runners, list) else [],
        ))
    return out


def _applies_to(record: LedgerRecord, key: str) -> bool:
    """Whether a ledger record speaks about one ``provider/model`` cell."""
    if record.scope == key:
        return True
    if record.scope == "all":
        return True
    if "/" not in record.scope and record.scope not in ("stance",):
        return key.split("/", 1)[0] == record.scope
    return False


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


@dataclass
class CellCoverage:
    week_id: str
    provider: str
    model_id: str
    status: str
    scheduled: bool
    prompts_measured: int
    prompts_total: int
    samples_published: int
    samples_expected: int | None
    samples_logged: int | None
    unusable_samples: int = 0
    rejected_samples: int = 0
    stance_cells: int | None = None
    stance_unscored: int | None = None
    reasons: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    unexplained: bool = False

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model_id}"

    @property
    def label(self) -> str:
        return STATUS_LABELS[self.status]


@dataclass
class WeekCoverage:
    week_id: str
    published: bool
    run_logged: bool
    recovery_notes: list[str]
    cells: list[CellCoverage]
    notes: list[LedgerRecord]
    #: The week's own committed manifest's marking, when it has one:
    #: ``partial``, ``notes``, per-runner ``coverage`` and ``corrections``
    #: (see ``schema.Manifest``). Empty for a week whose manifest is not
    #: in ``data/manifests`` or predates the keys.
    partial: bool = False
    manifest_notes: list[str] = field(default_factory=list)
    manifest_coverage: list[dict] = field(default_factory=list)
    corrections: list[dict] = field(default_factory=list)

    def corrected_by(self, record: LedgerRecord) -> list[LedgerRecord]:
        """Later ``corrected`` records that answer ``record``."""
        if record.kind not in _STATUS_KINDS:
            return []
        return [
            n for n in self.notes
            if n.kind == "corrected" and n.scope == record.scope
        ]

    @property
    def open_notes(self) -> list[LedgerRecord]:
        """Status-bearing week-level records not yet answered by a
        correction."""
        return [
            n for n in self.notes
            if n.kind in _STATUS_KINDS and not self.corrected_by(n)
        ]

    @property
    def has_warnings(self) -> bool:
        """Something a reader of this week's data must know: a due model
        short of complete, an open week-level ledger record, unmeasured
        stance, or a manifest marked partial."""
        return bool(self.open_notes) or self.partial or any(
            c.status not in ("ok", "not-scheduled") or c.stance_unscored
            for c in self.cells
        )

    @property
    def has_disclosures(self) -> bool:
        """Anything the week page must state: warnings, or a correction."""
        return self.has_warnings or bool(self.corrections) or any(
            n.kind == "corrected" for n in self.notes
        ) or any(n.kind in _STATUS_KINDS for n in self.notes)

    @property
    def has_findings(self) -> bool:
        return bool(self.notes) or any(
            c.status not in ("ok", "not-scheduled") or c.reasons or c.stance_unscored
            for c in self.cells
        )


@dataclass
class Coverage:
    weeks: list[WeekCoverage]
    models: list[tuple[str, str]]  # (provider, model_id), column order
    ledger: list[LedgerRecord]
    ledger_skipped: list[int]

    def week(self, week_id: str) -> "WeekCoverage | None":
        for w in self.weeks:
            if w.week_id == week_id:
                return w
        return None

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for w in self.weeks:
            for c in w.cells:
                out[c.status] += 1
        return out

    def rows(self) -> list[dict]:
        """Flat, machine-readable rows (one per scheduled or published
        cell), newest week first. Off-cadence cells are omitted: they
        carry no information beyond the roster."""
        out: list[dict] = []
        for w in self.weeks:
            for c in w.cells:
                if c.status == "not-scheduled":
                    continue
                d = asdict(c)
                d["evidence"] = [e["text"] for e in c.evidence]
                d["run_log_entry"] = w.run_logged
                out.append(d)
        return out


def _pair_sizes(manifest: Manifest) -> dict[str, int]:
    """Samples per (prompt, model) pair for each model: the largest cell
    ever captured, counting samples later excluded as unusable or never
    run because the provider declined them."""
    sizes: dict[str, int] = {}
    weeks = [(h.week_id, h.metrics) for h in manifest.history]
    weeks.append((manifest.snapshot.week_id, manifest.metrics))
    for _, metrics in weeks:
        for m in metrics:
            n = m.n_samples + (m.unusable_samples or 0) + (getattr(m, "rejected_samples", 0) or 0)
            sizes[m.model_id] = max(sizes.get(m.model_id, 0), n)
    return sizes


def _roster(row: dict) -> list[str]:
    """Same rule as ``check_run_health._roster``: ``expected_runners``
    when recorded, else the keys of ``per_runner_samples`` (seeded for
    every runner the run built, at 0, before the first request)."""
    expected = row.get("expected_runners")
    if isinstance(expected, list) and expected:
        return [str(r) for r in expected]
    per_runner = row.get("per_runner_samples")
    if isinstance(per_runner, dict):
        return [str(r) for r in per_runner]
    return []


def _int(v: object) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _week_manifest(manifests_dir: Path | None, week_id: str) -> dict:
    """The week's own committed manifest, ``data/manifests/<week>.json``,
    as plain JSON, or ``{}`` when there is none or it does not parse.

    Read raw rather than through ``Manifest`` so an older or newer file
    cannot fail the build. The week's own manifest is the published
    record of that week; the copy of it embedded in a later manifest's
    history is rebuilt on the pipeline host and does not carry stance,
    rejected requests or the partial marking.
    """
    if manifests_dir is None:
        return {}
    path = Path(manifests_dir) / f"{week_id}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _stance_counts(
    raw: dict, stance_axes: frozenset[str],
) -> dict[str, tuple[int, int]]:
    """``model_id -> (stance-bearing cells, cells the classifier failed
    on)`` from the week's committed manifest.

    Read from ``data/manifests/<week>.json`` because manifest history
    entries do not carry stance. A failed classifier call is published
    as stance ``na`` at confidence 0.0; a cell the classifier was
    deliberately not asked about carries 1.0.
    """
    axes = {p.get("prompt_id"): p.get("axis") for p in raw.get("prompts") or []}
    out: dict[str, list[int]] = {}
    for m in raw.get("metrics") or []:
        if axes.get(m.get("prompt_id")) not in stance_axes:
            continue
        slot = out.setdefault(str(m.get("model_id")), [0, 0])
        slot[0] += 1
        if m.get("stance") == "na" and m.get("stance_confidence") == 0.0:
            slot[1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def _excluded_counts(raw: dict) -> dict[str, tuple[int, int]]:
    """``model_id -> (unusable_samples, rejected_samples)`` from the
    week's committed manifest. History entries record these as 0 for
    weeks whose requests a provider declined, so the week's own file is
    the source when it exists."""
    out: dict[str, list[int]] = {}
    for m in raw.get("metrics") or []:
        slot = out.setdefault(str(m.get("model_id")), [0, 0])
        slot[0] += _int(m.get("unusable_samples"))
        slot[1] += _int(m.get("rejected_samples"))
    return {k: (v[0], v[1]) for k, v in out.items()}


def build_coverage(
    manifest: Manifest,
    *,
    run_log_rows: list[dict],
    ledger_raw: list[dict],
    ledger_skipped: list[int] | None = None,
    manifests_dir: Path | None = None,
    stance_axes: frozenset[str] = frozenset({"political", "historical-contested"}),
) -> Coverage:
    weeks_in_range = manifest.all_weeks
    in_range = set(weeks_in_range)
    ledger = [r for r in ledger_records(ledger_raw) if r.week_id in in_range]

    provider_of = {m.model_id: m.provider for m in manifest.models}
    published: dict[str, list] = {h.week_id: list(h.metrics) for h in manifest.history}
    published[manifest.snapshot.week_id] = list(manifest.metrics)
    unmeasured_current = {
        (u.model_id, u.prompt_id) for u in (manifest.unmeasured or [])
    }
    pair_sizes = _pair_sizes(manifest)
    default_prompts = len(manifest.prompts)

    rows_by_week: dict[str, list[dict]] = {}
    for row in run_log_rows:
        if row["week_id"] in in_range:
            rows_by_week.setdefault(row["week_id"], []).append(row)

    # Column order: first week a model appears in anything, then name.
    first_seen: dict[str, str] = {}

    def _see(key: str, week: str) -> None:
        if key not in first_seen or week < first_seen[key]:
            first_seen[key] = week

    weeks_out: list[WeekCoverage] = []
    for week in weeks_in_range:
        metrics = published.get(week, [])
        rows = rows_by_week.get(week, [])
        week_ledger = [r for r in ledger if r.week_id == week]

        roster: set[str] = set()
        logged: dict[str, int] = {}
        expected_logged: dict[str, int] = {}
        for row in rows:
            roster.update(_roster(row))
            for k, v in (row.get("per_runner_samples") or {}).items():
                logged[str(k)] = logged.get(str(k), 0) + _int(v)
            for k, v in (row.get("expected_samples") or {}).items():
                expected_logged[str(k)] = max(expected_logged.get(str(k), 0), _int(v))
        for rec in week_ledger:
            roster.update(rec.runners)
            if "/" in rec.scope and rec.kind in _STATUS_KINDS:
                roster.add(rec.scope)

        by_model: dict[str, list] = {}
        for m in metrics:
            by_model.setdefault(m.model_id, []).append(m)
        keys = set(roster)
        for model_id in by_model:
            keys.add(f"{provider_of.get(model_id, 'unknown')}/{model_id}")
        prompts_total = len({m.prompt_id for m in metrics}) or default_prompts
        if week == manifest.snapshot.week_id:
            prompts_total = max(
                prompts_total,
                len({m.prompt_id for m in metrics} | {p for _, p in unmeasured_current}),
            )
        own = _week_manifest(manifests_dir, week)
        stance = _stance_counts(own, stance_axes)
        excluded = _excluded_counts(own)

        cells: list[CellCoverage] = []
        for key in sorted(keys):
            provider, _, model_id = key.partition("/")
            _see(key, week)
            cell_metrics = by_model.get(model_id, [])
            samples = sum(m.n_samples for m in cell_metrics)
            if model_id in excluded:
                unusable, rejected = excluded[model_id]
            else:
                unusable = sum(m.unusable_samples or 0 for m in cell_metrics)
                rejected = sum(getattr(m, "rejected_samples", 0) or 0 for m in cell_metrics)
            measured = len({m.prompt_id for m in cell_metrics})
            expected = expected_logged.get(key)
            if expected is None and model_id in pair_sizes:
                expected = prompts_total * pair_sizes[model_id]
            due = key in roster or bool(cell_metrics)
            records = [
                r for r in week_ledger
                if r.scope not in ("stance",) and _applies_to(r, key)
            ]
            if not due:
                records = [r for r in records if r.scope == key]
            status_kinds = [r.kind for r in records if r.kind in _STATUS_KINDS]
            unexplained = False
            if status_kinds:
                status = min(status_kinds, key=STATUSES.index)
            elif not due:
                status = "not-scheduled"
            elif samples > 0:
                status = "partial" if measured < prompts_total else "ok"
                # A complete-looking cell short of its expected samples
                # that no unusable or declined sample accounts for is a
                # loss nobody has explained yet (2026-W33 gpt-5.5 had a
                # failed pair before rejections were counted).
                unexplained = status == "partial" or bool(
                    expected and samples + unusable + rejected < expected
                )
            elif logged.get(key, 0) > 0:
                status = "scheduled"
                unexplained = True
            else:
                status = "lost"
                unexplained = True
            reasons = [r.reason for r in records if r.scope != "all" or r.kind not in _INFO_KINDS]
            evidence: list[dict] = []
            for r in records:
                if r.scope == "all" and r.kind in _INFO_KINDS:
                    continue
                for e in r.evidence:
                    if e not in evidence:
                        evidence.append(e)
            st = stance.get(model_id)
            cells.append(CellCoverage(
                week_id=week,
                provider=provider,
                model_id=model_id,
                status=status,
                scheduled=due,
                prompts_measured=measured,
                prompts_total=prompts_total,
                samples_published=samples,
                samples_expected=expected if due else None,
                samples_logged=(logged.get(key, 0) if rows else None),
                unusable_samples=unusable,
                rejected_samples=rejected,
                stance_cells=st[0] if st else None,
                stance_unscored=st[1] if st else None,
                reasons=reasons,
                evidence=evidence,
                unexplained=unexplained,
            ))

        notes = [
            r for r in week_ledger
            if r.scope == "stance" or (r.scope == "all" and r.kind in _INFO_KINDS)
            or ("/" in r.scope and not any(c.key == r.scope for c in cells))
        ]
        recovery_notes = [
            str(row.get("note")) for row in rows
            if row.get("recovery") and row.get("note")
        ]
        weeks_out.append(WeekCoverage(
            week_id=week,
            published=week in published,
            run_logged=bool(rows),
            recovery_notes=recovery_notes,
            cells=cells,
            notes=notes,
            partial=own.get("partial") is True,
            manifest_notes=[str(n) for n in own.get("notes") or []],
            manifest_coverage=[c for c in own.get("coverage") or [] if isinstance(c, dict)],
            corrections=[c for c in own.get("corrections") or [] if isinstance(c, dict)],
        ))

    models = sorted(first_seen, key=lambda k: (first_seen[k], k))
    model_cols = [tuple(k.split("/", 1)) for k in models]
    # Every week gets every column, so the table is a true matrix.
    for w in weeks_out:
        have = {c.key for c in w.cells}
        for provider, model_id in model_cols:
            if f"{provider}/{model_id}" not in have:
                w.cells.append(CellCoverage(
                    week_id=w.week_id, provider=provider, model_id=model_id,
                    status="not-scheduled", scheduled=False,
                    prompts_measured=0, prompts_total=0,
                    samples_published=0, samples_expected=None,
                    samples_logged=None,
                ))
        order = {f"{p}/{m}": i for i, (p, m) in enumerate(model_cols)}
        w.cells.sort(key=lambda c: order.get(c.key, len(order)))

    weeks_out.reverse()  # newest first, like /data/
    return Coverage(
        weeks=weeks_out,
        models=model_cols,  # type: ignore[arg-type]
        ledger=sorted(ledger, key=lambda r: r.week_id),
        ledger_skipped=list(ledger_skipped or []),
    )


def coverage_csv(cov: Coverage) -> str:
    import csv
    import io

    cols = [
        "week_id", "provider", "model_id", "status", "prompts_measured",
        "prompts_total", "samples_published", "samples_expected",
        "samples_logged", "unusable_samples", "rejected_samples",
        "stance_cells", "stance_unscored", "run_log_entry", "reasons",
        "evidence",
    ]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for row in cov.rows():
        out = []
        for c in cols:
            v = row.get(c)
            if isinstance(v, list):
                v = " | ".join(str(x) for x in v)
            elif isinstance(v, bool):
                v = str(v).lower()
            out.append("" if v is None else v)
        w.writerow(out)
    return buf.getvalue()


def coverage_jsonl(cov: Coverage) -> str:
    lines = [json.dumps(r, sort_keys=True) for r in cov.rows()]
    return "\n".join(lines) + ("\n" if lines else "")
