"""Actual-cost tracker based on stored Sample token counts.

The pre-flight :mod:`meridian.sampling.pricing` estimator works from
average-length heuristics. This module computes the *actual* cost of a
run from the token counts each :class:`Sample` carries after capture.

Priced to the same table as the estimator; if a runner's token counts
are missing (some providers are flaky about reporting them), that
(prompt × model × week) simply contributes zero to the actual-cost total.
"""
from __future__ import annotations

from dataclasses import dataclass

from meridian.runners.base import Sample
from meridian.sampling.pricing import PRICING


@dataclass(frozen=True)
class CostReport:
    total_usd: float
    by_runner: dict[str, float]
    samples_priced: int
    samples_skipped_no_tokens: int


def _price_for(provider: str, model_id: str) -> tuple[float, float] | None:
    return PRICING.get((provider, model_id)) or PRICING.get((provider, "*"))


def compute_actual_cost(samples: list[Sample]) -> CostReport:
    by_runner: dict[str, float] = {}
    priced = 0
    skipped = 0
    total = 0.0
    for s in samples:
        if s.input_tokens is None or s.output_tokens is None:
            skipped += 1
            continue
        pricing = _price_for(s.provider, s.model_id)
        if pricing is None:
            # Unknown model; treat as free. Better than making up a price.
            priced += 1
            continue
        in_usd, out_usd = pricing
        cost = (
            (s.input_tokens / 1_000_000) * in_usd
            + (s.output_tokens / 1_000_000) * out_usd
        )
        key = f"{s.provider}/{s.model_id}"
        by_runner[key] = round(by_runner.get(key, 0.0) + cost, 6)
        total += cost
        priced += 1
    return CostReport(
        total_usd=round(total, 4),
        by_runner={k: round(v, 4) for k, v in by_runner.items()},
        samples_priced=priced,
        samples_skipped_no_tokens=skipped,
    )
