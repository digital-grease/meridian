"""Pipeline -> site contract.

Any change that would break an existing manifest must bump SCHEMA_VERSION.
The site build fails loudly on version mismatch rather than producing
silently-broken pages.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

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


class PromptRecord(Frozen):
    prompt_id: str
    axis: Axis
    title: str
    text_hash: str  # sha256 of canonical prompt text
    description: str | None = None
    held_out: bool = False


class ModelRecord(Frozen):
    model_id: str           # stable slug, e.g. "claude-opus-4-7"
    display_name: str
    provider: str
    version_string: str     # exact provider-reported version at capture time
    available: bool = True


class MetricRecord(Frozen):
    prompt_id: str
    model_id: str
    n_samples: int = Field(ge=0)
    refusal_rate: float = Field(ge=0.0, le=1.0)
    refusal_ci: ConfidenceInterval
    hedge_density: float = Field(ge=0.0)
    length: LengthStats
    stance: Stance = "na"
    stance_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    embedding_centroid_shift: float | None = Field(default=None, ge=0.0)
    sample_s3_uris: list[str] = Field(default_factory=list)
    flagged_for_review: bool = False
    flag_reason: str | None = None


class Snapshot(Frozen):
    week_id: str            # ISO week, e.g. "2026-W16"
    generated_at: datetime
    corpus_git_sha: str
    pipeline_version: str


class HistorySnapshot(Frozen):
    """A prior week's metrics. Models and prompts are referenced by id
    into the current manifest; the history field carries just the numbers."""
    week_id: str
    generated_at: datetime
    metrics: list[MetricRecord]


class Manifest(Frozen):
    schema_version: int
    snapshot: Snapshot
    models: list[ModelRecord]
    prompts: list[PromptRecord]
    metrics: list[MetricRecord]
    history: list[HistorySnapshot] = Field(default_factory=list)  # oldest-first
    flagged: list[str] = Field(default_factory=list)

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
