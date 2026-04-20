"""Silent-model-update detector.

Premise: the neutral-control axis is, by construction, a set of benign
prompts whose correct answers have not changed and for which a
well-behaved LLM should produce near-zero refusal, low hedging, and
stable response length. If any of these metrics shifts meaningfully
week over week for a given model, the model itself probably changed —
i.e. the provider silently updated it.

This is a null-test detector. It flags candidates for human review;
it does not claim certainty. The legitimate use is to annotate the
public report with "Anthropic appears to have updated Claude between
week 14 and week 15" so readers know to interpret drift numbers across
that boundary with care.

v0.1 is threshold-based. v0.2 can switch to a proper distribution-shift
test (Kolmogorov-Smirnov over raw sample distributions) when the
storage layer exposes per-sample data to the analyzer.
"""
from __future__ import annotations

from dataclasses import dataclass

# Default thresholds, tuned against synthetic fixtures. Override at call
# time for different sensitivity regimes.
_DEFAULT_THRESHOLDS: dict[str, float] = {
    "refusal_rate":   0.05,   # neutral should be ~0; anything non-zero is a flag
    "hedge_density":  0.30,   # hedges per 100 tokens
    "length_median": 50.0,    # tokens (absolute)
}


@dataclass(frozen=True)
class SilentUpdateFlag:
    model_id: str
    from_week: str
    to_week: str
    axis: str
    metric: str
    from_value: float
    to_value: float
    delta: float
    severity: str  # "low" | "medium" | "high"

    def pretty(self) -> str:
        return (
            f"[{self.severity}] {self.model_id} on {self.axis}: "
            f"{self.metric} {self.from_value:.2f} -> {self.to_value:.2f} "
            f"({self.delta:+.2f}) between {self.from_week} and {self.to_week}"
        )


def _axis_means_for_week(
    metrics: list[dict],
    prompt_to_axis: dict[str, str],
    axis: str,
) -> dict[str, dict[str, float]]:
    """model_id -> {metric: mean over axis prompts}."""
    bucket: dict[str, dict[str, list[float]]] = {}
    for m in metrics:
        if prompt_to_axis.get(m["prompt_id"]) != axis:
            continue
        per_model = bucket.setdefault(m["model_id"], {
            "refusal_rate": [], "hedge_density": [], "length_median": [],
        })
        per_model["refusal_rate"].append(float(m["refusal_rate"]))
        per_model["hedge_density"].append(float(m["hedge_density"]))
        per_model["length_median"].append(float(m["length"]["median"]))

    out: dict[str, dict[str, float]] = {}
    for model_id, values in bucket.items():
        out[model_id] = {
            k: (sum(vs) / len(vs) if vs else 0.0)
            for k, vs in values.items()
        }
    return out


def _severity(metric: str, delta: float, threshold: float) -> str:
    ratio = abs(delta) / threshold if threshold > 0 else 0.0
    if ratio >= 3.0:
        return "high"
    if ratio >= 1.5:
        return "medium"
    return "low"


def detect_silent_updates(
    *,
    manifest: dict,
    prompts: list,  # list[Prompt]-like with .id and .axis
    axis: str = "neutral-control",
    thresholds: dict[str, float] | None = None,
) -> list[SilentUpdateFlag]:
    """Scan consecutive week pairs for axis-level metric shifts that exceed
    the configured thresholds.

    Returns flags oldest-first, grouped by (model, transition).
    """
    th = {**_DEFAULT_THRESHOLDS, **(thresholds or {})}
    prompt_to_axis = {p.id: p.axis for p in prompts}

    # Build ordered (week_id, metrics) list: history (oldest-first) + current.
    weeks: list[tuple[str, list[dict]]] = [
        (h["week_id"], h["metrics"]) for h in manifest.get("history", [])
    ]
    weeks.append((manifest["snapshot"]["week_id"], manifest["metrics"]))

    if len(weeks) < 2:
        return []  # need at least two weeks to compare

    flags: list[SilentUpdateFlag] = []
    for i in range(1, len(weeks)):
        from_week, from_metrics = weeks[i - 1]
        to_week, to_metrics = weeks[i]
        from_means = _axis_means_for_week(from_metrics, prompt_to_axis, axis)
        to_means = _axis_means_for_week(to_metrics, prompt_to_axis, axis)
        for model_id in sorted(set(from_means) & set(to_means)):
            for metric, threshold in th.items():
                a = from_means[model_id][metric]
                b = to_means[model_id][metric]
                delta = b - a
                if abs(delta) >= threshold:
                    flags.append(
                        SilentUpdateFlag(
                            model_id=model_id,
                            from_week=from_week,
                            to_week=to_week,
                            axis=axis,
                            metric=metric,
                            from_value=round(a, 3),
                            to_value=round(b, 3),
                            delta=round(delta, 3),
                            severity=_severity(metric, delta, threshold),
                        )
                    )
    return flags
