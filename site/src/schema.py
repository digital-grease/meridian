"""Pipeline -> site contract.

Any change that would break an existing manifest must bump SCHEMA_VERSION.
The site build fails loudly on version mismatch rather than producing
silently-broken pages.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 2

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
    median: float = Field(ge=0.0)
    p25: float = Field(ge=0.0)
    p75: float = Field(ge=0.0)
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
    refusal_rate: float = Field(ge=0.0, le=1.0)
    refusal_ci: ConfidenceInterval
    hedge_density: float = Field(ge=0.0)
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
    def all_weeks(self) -> list[str]:
        """All week_ids present (history + current), oldest-first."""
        return [h.week_id for h in self.history] + [self.snapshot.week_id]

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
        """
        for m in self.metrics:
            if m.prompt_id == prompt_id and m.model_id == model_id:
                return list(getattr(m.change_points, metric))
        return []

    def timeseries(self, prompt_id: str, model_id: str, metric: str) -> list[tuple[str, float]]:
        """(week_id, value) points for a given (prompt, model, metric), oldest-first.

        metric is one of: 'refusal_rate', 'hedge_density', 'length_median'.
        """
        points: list[tuple[str, float]] = []
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
                    points.append((snap_week, v))
                    break
        return points
