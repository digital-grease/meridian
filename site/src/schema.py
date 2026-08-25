"""Pipeline -> site contract.

Any change that would break an existing manifest must bump SCHEMA_VERSION.
The site build fails loudly on version mismatch rather than producing
silently-broken pages.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 2

_WEEK_ID_RE = re.compile(r"^(\d{4})-W(\d{2})$")


class MissingWeek:
    """Sentinel for an ISO week in which the audit captured nothing at all.

    Distinct from "this model was not sampled that week", which is the
    normal consequence of the biweekly frontier cadence and shows up as
    a week simply having no point for that series. This means *no*
    runner produced samples that week, so the week has no snapshot in
    the manifest at all and never will.

    It exists because of 2026-W30 and 2026-W31, when the weekly run
    never started (the orchestrator's EC2 request was rejected with
    ``InsufficientInstanceCapacity`` two Mondays running) and no data
    was captured. Before this sentinel, :meth:`Manifest.timeseries`
    simply skipped those weeks, so a chart drew W29 and W32 as adjacent
    points joined by a single unbroken line. That is the one thing a
    longitudinal record must never do: it renders a two-week hole as
    continuity, and the methodology page was simultaneously promising
    readers "a break in the line".

    The sentinel is a distinct object rather than ``None`` so it can
    carry its own rendering. Templates format series values through
    ``"{:.2f}".format(...)``; formatting ``None`` raises, while this
    formats to the same em dash the tables already use for "no value"
    regardless of the format spec. It is falsy so ``{% if value %}``
    guards treat it as absent.
    """

    __slots__ = ()

    _TEXT = "—"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING_WEEK"

    def __str__(self) -> str:
        return self._TEXT

    def __bool__(self) -> bool:
        return False

    def __format__(self, format_spec: str) -> str:
        return self._TEXT


#: Singleton instance. Compare with ``is MISSING_WEEK``.
MISSING_WEEK = MissingWeek()


def is_measured(value: object) -> bool:
    """True when a time-series value is a real number rather than a gap.

    Booleans are excluded deliberately: ``bool`` is a subclass of
    ``int`` and a stray ``True`` in a metric series would otherwise be
    plotted as 1.0.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _week_start(week_id: str) -> date | None:
    """Monday of ``week_id`` (``"2026-W30"``), or None if unparseable.

    Returning None rather than raising is deliberate: an unrecognised
    week label must degrade to "we cannot reason about calendar gaps
    here", never to a failed site build. The manifest is the pipeline's
    output, not user input, but a malformed label is exactly the kind of
    thing that should not take the public record offline.
    """
    m = _WEEK_ID_RE.match(week_id)
    if not m:
        return None
    year, week = int(m.group(1)), int(m.group(2))
    try:
        return date.fromisocalendar(year, week, 1)
    except ValueError:
        return None


def _format_week(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def week_span(first: str, last: str) -> list[str]:
    """Every ISO week id from ``first`` to ``last`` inclusive, oldest-first.

    Steps by calendar week rather than by integer arithmetic on the week
    number so 52/53-week years and year boundaries come out right.
    Returns ``[]`` when either label is unparseable or the range is
    inverted, so callers can fall back to the weeks they observed.
    """
    start, end = _week_start(first), _week_start(last)
    if start is None or end is None or end < start:
        return []
    out: list[str] = []
    cursor = start
    while cursor <= end:
        out.append(_format_week(cursor))
        cursor += timedelta(days=7)
    return out

Axis = Literal[
    "political",
    "historical-contested",
    "scientific-consensus",
    "refusal-boundary",
    "neutral-control",
    "factual-stability",
]

Stance = Literal["pro", "anti", "neutral", "na"]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConfidenceInterval(Frozen):
    lower: float = Field(ge=0.0, le=1.0)
    upper: float = Field(ge=0.0, le=1.0)


class LengthStats(Frozen):
    """Response-length distribution over the samples that carried text.

    The three quantiles are nullable, and null is the only honest value
    when ``n == 0``. That case arrived with the 2026-W32 Anthropic
    change: a cell can be fully sampled and fully measured (20 usable
    samples, refusal rate 1.00) while carrying no text at all, because
    every refusal came back in ``stop_reason`` with an empty body. The
    length summary of an empty list used to be ``0.0`` for all three
    quantiles, which reads as "the model answered with zero words" and
    is the fabricated-zero failure ``meridian.analysis.usability``
    exists to prevent. Only ``n`` was ever pinned at 0, and nothing
    downstream looked at it, so the zeros flowed into the CSV export and
    into the averaged axis figures on the home-page heatmap.

    Nullable rather than absent so that ``n`` still states how many
    responses the distribution was computed from, and defaulted so every
    manifest published before 2026-08 stays valid.
    """
    median: float | None = Field(default=None, ge=0.0)
    p25: float | None = Field(default=None, ge=0.0)
    p75: float | None = Field(default=None, ge=0.0)
    n: int = Field(ge=0)


class DriftTest(Frozen):
    """Two-sample drift test result for one metric on one (prompt × model).

    Populated only when a prior week's samples are available. The
    ``adjusted_p_value`` and ``significant_after_bh`` fields are filled
    in by the within-week Benjamini–Hochberg pass in
    ``meridian.pipeline.manifest_writer``; ``p_value`` alone is the
    raw output of ``meridian.analysis.drift_tests``.
    """
    p_value: float = Field(ge=0.0, le=1.0)
    adjusted_p_value: float = Field(ge=0.0, le=1.0)
    significant_after_bh: bool
    #: The week this test compared the current week against, e.g.
    #: ``"2026-W28"``. A drift p-value is meaningless without it: the
    #: comparison baseline is normally the previous week the pair ran,
    #: but the biweekly frontier cadence and the 2026-W30/W31 outage
    #: both push it further back, and a reader seeing only "p = 0.03"
    #: would reasonably assume week-over-week.
    #: Optional because every manifest published from 2026-W17 through
    #: 2026-W32 predates the field.
    compared_to_week: str | None = None
    #: Calendar weeks between ``compared_to_week`` and the snapshot
    #: week. 1 means a true week-over-week test; anything larger means
    #: the "drift" accumulated over a longer interval and should be
    #: read accordingly. Optional for the same reason as above.
    weeks_elapsed: int | None = Field(default=None, ge=1)


class ChangePointsSummary(Frozen):
    """Precomputed PELT change-point indices per metric time series.

    Indices point into the oldest-first series returned by
    :meth:`Manifest.timeseries`. A non-terminal segment boundary at
    position ``k`` means the metric changed regime starting at week
    ``series[k]``. Computed once by the pipeline; the site consumes
    these indices directly rather than invoking the analysis code at
    render time.
    """
    refusal_rate: list[int] = Field(default_factory=list)
    hedge_density: list[int] = Field(default_factory=list)
    length_median: list[int] = Field(default_factory=list)


class PromptRecord(Frozen):
    prompt_id: str
    axis: Axis
    title: str
    text_hash: str  # sha256 of canonical prompt text
    description: str | None = None
    held_out: bool = False


class ModelRecord(Frozen):
    model_id: str           # stable slug, e.g. "claude-opus-4-8"
    display_name: str
    provider: str
    version_string: str     # exact provider-reported version at capture time
    available: bool = True


class MetricRecord(Frozen):
    prompt_id: str
    model_id: str
    #: Samples the metrics on this record were computed from. Excludes
    #: any that returned no usable content — see ``unusable_samples``.
    n_samples: int = Field(ge=0)
    #: Samples captured for this cell that carried no measurement and
    #: were excluded (empty body, typically a completion truncated by
    #: the token cap before emitting output). Defaulted to 0 so
    #: manifests published before 2026-07-24 stay valid.
    unusable_samples: int = Field(default=0, ge=0)
    #: Requests the provider declined to run for this cell, so they never
    #: became samples. Excluded from every metric and reported only.
    #:
    #: Distinct from ``unusable_samples`` because the two describe
    #: different losses: that one is a response we received and could not
    #: measure, this one is a response that does not exist. Keeping them
    #: apart is what lets a reader tell "the model answered and we could
    #: not score it" from "the platform would not run the prompt", which
    #: are opposite findings and only one is about the model.
    #:
    #: Defaulted to 0 so manifests published before 2026-08-25 stay
    #: valid; a default keeps this a widening change, so SCHEMA_VERSION
    #: does not move.
    rejected_samples: int = Field(default=0, ge=0)
    refusal_rate: float = Field(ge=0.0, le=1.0)
    refusal_ci: ConfidenceInterval
    #: None when the cell carried no response text to measure. Same
    #: reasoning as the nullable ``LengthStats`` quantiles above, and it
    #: has to be nullable for the same reason: ``hedge.density("")``
    #: returns 0.0 as a divide-by-zero sentinel, and a published 0.0
    #: asserts "this model hedged in none of its answers" about a cell
    #: that produced no answers to hedge in. A provider refusal
    #: delivered as ``stop_reason='refusal'`` with an empty body is
    #: exactly that shape. Defaulted so every manifest published before
    #: 2026-08 stays valid.
    hedge_density: float | None = Field(default=None, ge=0.0)
    length: LengthStats
    stance: Stance = "na"
    stance_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    embedding_centroid_shift: float | None = Field(default=None, ge=0.0)
    refusal_drift: DriftTest | None = None
    hedge_drift: DriftTest | None = None
    length_drift: DriftTest | None = None
    change_points: ChangePointsSummary = Field(default_factory=ChangePointsSummary)
    # `sample_s3_uris` lived here until 2026-07-25. It was never
    # populated by the pipeline and no template ever read it, and the
    # URIs it was meant to hold pointed into the private archive bucket,
    # so publishing them would have given readers links they could not
    # fetch. Sample responses are now shown directly on each prompt page
    # (see site/src/excerpts.py), sourced from the public per-week
    # snapshot. The field was removed rather than left empty so the
    # schema stops advertising a capability that does not exist.
    flagged_for_review: bool = False
    flag_reason: str | None = None


class UnmeasuredCell(Frozen):
    """A (prompt × model) cell that was sampled but yielded no metric.

    Distinct from a cell that is simply absent from ``metrics``. Absent
    means the model was not run that week, normally because the frontier
    roster alternates by ISO-week parity. This means we did run it, the
    requests succeeded, and every response came back with no usable
    content, so there is nothing honest to publish as a number.

    The distinction is the whole point: a refusal rate of 0.00 and a
    median length of 0 for a model that answered nothing would read as
    "complied fully, wrote nothing" to anyone consuming the data.
    """
    prompt_id: str
    model_id: str
    unusable_samples: int = Field(ge=0)
    #: Reason code -> count, from ``meridian.analysis.usability``.
    reasons: dict[str, int] = Field(default_factory=dict)


class Snapshot(Frozen):
    week_id: str            # ISO week, e.g. "2026-W16"
    generated_at: datetime
    #: Commit that last changed the public corpus file. Scoped to that
    #: file, not repo HEAD, so it only moves when the prompts do.
    #: ``"unknown"`` on manifests built outside a git checkout, and
    #: suffixed ``-dirty`` when built against uncommitted corpus edits.
    corpus_git_sha: str
    #: The corpus's own declared version from ``prompts.yaml``, e.g.
    #: ``"2026.04.19-v0.2"``. Human-readable counterpart to the sha, and
    #: the identifier the corpus-versioning contract is written in
    #: (prompts are superseded across versions, never edited in place).
    #: Defaulted for manifests published before 2026-07-25, which
    #: predate the field.
    corpus_version: str = "unknown"
    pipeline_version: str


class HistorySnapshot(Frozen):
    """A prior week's metrics. Models and prompts are referenced by id
    into the current manifest; the history field carries just the numbers."""
    week_id: str
    generated_at: datetime
    metrics: list[MetricRecord]


class SilentUpdateWarning(Frozen):
    """Candidate silent-model-update event on the neutral-control axis.

    Flagged when a model's axis-level metric shifts week-over-week by
    more than ``meridian.analysis.silent_update`` thresholds. These
    are *candidates*, not proven updates — the public report should say
    "appears to have updated" and invite human review.
    """
    model_id: str
    from_week: str
    to_week: str
    axis: str
    metric: str
    from_value: float
    to_value: float
    delta: float
    severity: Literal["low", "medium", "high"]


class Manifest(Frozen):
    schema_version: int
    snapshot: Snapshot
    models: list[ModelRecord]
    prompts: list[PromptRecord]
    metrics: list[MetricRecord]
    history: list[HistorySnapshot] = Field(default_factory=list)  # oldest-first
    unmeasured: list[UnmeasuredCell] = Field(default_factory=list)
    flagged: list[str] = Field(default_factory=list)
    silent_update_warnings: list[SilentUpdateWarning] = Field(default_factory=list)

    def unmeasured_for(self, pid: str, mid: str) -> UnmeasuredCell | None:
        return next(
            (
                u for u in self.unmeasured
                if u.prompt_id == pid and u.model_id == mid
            ),
            None,
        )

    def prompt_by_id(self, pid: str) -> PromptRecord | None:
        return next((p for p in self.prompts if p.prompt_id == pid), None)

    def model_by_id(self, mid: str) -> ModelRecord | None:
        return next((m for m in self.models if m.model_id == mid), None)

    def metrics_for_prompt(self, pid: str) -> list[MetricRecord]:
        return [m for m in self.metrics if m.prompt_id == pid]

    def metrics_for_model(self, mid: str) -> list[MetricRecord]:
        return [m for m in self.metrics if m.model_id == mid]

    @property
    def observed_weeks(self) -> list[str]:
        """Week_ids the audit actually captured (history + current), oldest-first.

        These are the weeks that have a snapshot, and therefore the only
        weeks with a ``/data/{week}/`` payload.
        """
        return [h.week_id for h in self.history] + [self.snapshot.week_id]

    @property
    def missing_weeks(self) -> list[str]:
        """ISO weeks inside the observation window with no snapshot at all.

        The window runs from the oldest observed week to the snapshot
        week, so this never invents weeks before the audit started or
        after the current one. Empty when the record is contiguous.

        Populated for the first time by the 2026-W30 and 2026-W31
        outages, when two consecutive weekly runs never started and no
        samples exist for any model. Those weeks are real holes in a
        longitudinal record and every surface that draws a time axis has
        to show them, so this is computed here once rather than
        re-derived by each caller.
        """
        observed = self.observed_weeks
        if len(observed) < 2:
            return []
        span = week_span(observed[0], observed[-1])
        if not span:
            return []
        present = set(observed)
        return [w for w in span if w not in present]

    @property
    def all_weeks(self) -> list[str]:
        """Every ISO week in the observation window, oldest-first.

        Includes weeks the audit did not run (see :attr:`missing_weeks`)
        so that any table or chart keyed on this list has a horizontal
        axis that is true to the calendar. A missing week keeps its own
        column, showing the hole, instead of being silently closed up by
        putting the weeks either side of it next to each other.

        Use :attr:`observed_weeks` when you need "weeks that actually
        have data" — for example when deciding which ``/data/{week}/``
        payloads to publish.
        """
        observed = self.observed_weeks
        missing = self.missing_weeks
        if not missing:
            return observed
        return sorted(observed + missing)

    def current_metric(self, prompt_id: str, model_id: str) -> MetricRecord | None:
        """The current-week MetricRecord for a (prompt, model) pair, if any."""
        for m in self.metrics:
            if m.prompt_id == prompt_id and m.model_id == model_id:
                return m
        return None

    def change_points_for(self, prompt_id: str, model_id: str, metric: str) -> list[int]:
        """Precomputed change-point indices for a (prompt × model × metric) series.

        Returns indices into :meth:`timeseries` output for the same
        triple. ``metric`` is one of ``'refusal_rate'``, ``'hedge_density'``,
        ``'length_median'``. Empty list when no change points were
        detected or the series is too short.

        The pipeline computes these against the *measured* series, which
        has no entries for weeks the audit did not run. Since 2026-W30
        :meth:`timeseries` interleaves gap sentinels for those weeks, so
        a raw index of 4 no longer necessarily lands on the fifth
        measurement. The indices are remapped here, at the one place
        that owns the contract, rather than leaving every caller to
        discover the offset. Out-of-range indices are dropped: a marker
        drawn at the wrong week is worse than no marker.
        """
        raw: list[int] = []
        for m in self.metrics:
            if m.prompt_id == prompt_id and m.model_id == model_id:
                raw = list(getattr(m.change_points, metric))
                break
        if not raw:
            return []
        series = self.timeseries(prompt_id, model_id, metric)
        measured_positions = [
            i for i, (_week, value) in enumerate(series) if is_measured(value)
        ]
        return [
            measured_positions[idx]
            for idx in raw
            if isinstance(idx, int) and 0 <= idx < len(measured_positions)
        ]

    def sampled_but_null_weeks(
        self, prompt_id: str, model_id: str, metric: str
    ) -> set[str]:
        """Weeks where this pair WAS sampled but ``metric`` is null.

        Distinguishes the third state a per-week table has to render.
        :meth:`timeseries` drops null values, so without this a cell that
        was sampled twenty times and produced a measured refusal rate of
        1.00 falls through to the same "not sampled this week" branch as
        a model that was off-cadence. On the 2026-W32 claude-opus-4-8
        row for ``ref-pipe-bomb-construct`` that would have put "not
        sampled" directly under a published refusal rate of 1.00, on the
        one cell the whole correction exists to surface.

        Walks history and the current week the same way
        :meth:`timeseries` does, so the two always agree about which
        weeks hold a record.
        """
        out: set[str] = set()
        for snap_week, snap_metrics in [
            *[(h.week_id, h.metrics) for h in self.history],
            (self.snapshot.week_id, self.metrics),
        ]:
            for m in snap_metrics:
                if m.prompt_id != prompt_id or m.model_id != model_id:
                    continue
                if metric == "refusal_rate":
                    v = m.refusal_rate
                elif metric == "hedge_density":
                    v = m.hedge_density
                elif metric == "length_median":
                    v = m.length.median
                else:
                    raise ValueError(f"unknown metric: {metric}")
                if v is None:
                    out.add(snap_week)
        return out

    def timeseries(
        self, prompt_id: str, model_id: str, metric: str
    ) -> list[tuple[str, float | MissingWeek]]:
        """(week_id, value) points for a given (prompt, model, metric), oldest-first.

        metric is one of: 'refusal_rate', 'hedge_density', 'length_median'.

        Weeks the audit never ran (:attr:`missing_weeks`) are carried
        through as :data:`MISSING_WEEK` rather than skipped, so a
        consumer can tell "no run that week" from "the series simply
        continues". :func:`is_measured` is the test for a real value.

        Only *interior* gaps are emitted. A gap sentinel exists to break
        a line between two measurements, so leading and trailing ones
        would describe nothing; suppressing them also keeps
        ``values[-1]`` meaning "the latest measurement", which is what
        the model page's Latest column reads.

        Weeks where this particular pair was not sampled but the audit
        did run are still skipped, not sentinelled. That is the biweekly
        frontier cadence working as designed, not a hole in the record,
        and conflating the two would reduce every alternating model's
        chart to a row of disconnected dots.

        A record whose metric is null carries no point either. Since
        2026-W32 ``length.median`` is null on a cell that was measured
        but produced no text (see :class:`LengthStats`); there is no
        length to plot, and substituting the old 0.0 would draw a
        collapse in response length that never happened. The pipeline
        computes change-point indices over the same non-null series, so
        :meth:`change_points_for` stays aligned.
        """
        points: list[tuple[str, float | MissingWeek]] = []
        for snap_week, snap_metrics in [
            *[(h.week_id, h.metrics) for h in self.history],
            (self.snapshot.week_id, self.metrics),
        ]:
            for m in snap_metrics:
                if m.prompt_id == prompt_id and m.model_id == model_id:
                    if metric == "refusal_rate":
                        v = m.refusal_rate
                    elif metric == "hedge_density":
                        v = m.hedge_density
                    elif metric == "length_median":
                        v = m.length.median
                    else:
                        raise ValueError(f"unknown metric: {metric}")
                    if v is not None:
                        points.append((snap_week, v))
                    break
        if not points:
            return points
        interior = [
            w for w in self.missing_weeks
            if points[0][0] < w < points[-1][0]
        ]
        if not interior:
            return points
        gapped = points + [(w, MISSING_WEEK) for w in interior]
        gapped.sort(key=lambda pair: pair[0])
        return gapped
