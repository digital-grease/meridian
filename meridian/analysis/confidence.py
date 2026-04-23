"""Bootstrap confidence intervals.

Used to attach a 95% CI to every published refusal rate so readers can tell
noise from signal. 1000 bootstrap rounds is standard for Bernoulli rates
and keeps the computation cheap.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class ConfidenceInterval:
    lower: float
    upper: float


def bootstrap_ci(
    observations: list[float],
    *,
    rounds: int = 1000,
    ci: float = 0.95,
    seed: int | None = None,
) -> ConfidenceInterval:
    """Percentile bootstrap CI on the mean of ``observations``.

    ``observations`` can be 0/1 (for refusal rate) or continuous. Uses a
    local RNG so callers can opt into determinism via ``seed`` without
    perturbing global random state.
    """
    if not observations:
        return ConfidenceInterval(lower=0.0, upper=0.0)
    rng = random.Random(seed) if seed is not None else random.Random()
    n = len(observations)
    means: list[float] = []
    for _ in range(rounds):
        sample = [observations[rng.randrange(n)] for _ in range(n)]
        means.append(statistics.mean(sample))
    means.sort()
    tail = (1.0 - ci) / 2.0
    lo_idx = max(0, int(rounds * tail))
    hi_idx = min(rounds - 1, int(rounds * (1.0 - tail)))
    return ConfidenceInterval(
        lower=round(means[lo_idx], 4),
        upper=round(means[hi_idx], 4),
    )
