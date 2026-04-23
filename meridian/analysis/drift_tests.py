"""Two-sample drift tests per (prompt × model × metric).

Produces the p-values consumed by
:mod:`meridian.analysis.multiple_testing` for within-week BH correction.
See ``STATISTICS.md`` for the methodology and why bootstrap/permutation
rather than closed-form tests.

Each helper takes this week's and the prior week's
:class:`~meridian.runners.base.Sample` lists and returns a two-sided
p-value under the null "the two samples come from the same underlying
distribution." Returns ``None`` when either side is empty (the pair is
then excluded from the BH family — it is not treated as ``p=1.0``).
"""
from __future__ import annotations

import random
import statistics
from typing import Callable

from meridian.analysis.hedge import hedge_density
from meridian.analysis.refusal import classify_refusal
from meridian.runners.base import Sample


def permutation_two_sample_pvalue(
    current: list[float],
    prior: list[float],
    *,
    stat: Callable[[list[float]], float] = statistics.mean,
    rounds: int = 1000,
    rng: random.Random | None = None,
) -> float | None:
    """Two-sided permutation p-value for ``stat(current) - stat(prior)``.

    Shuffles the pooled observations ``rounds`` times, re-splits into
    groups of the original sizes, and reports the fraction of
    null-distribution ``|Δ|`` values at least as extreme as the observed
    one. Inclusive on both ends, so the returned p is always in
    ``[1/rounds, 1.0]``.
    """
    if not current or not prior:
        return None
    rng = rng or random.Random()
    observed = abs(stat(current) - stat(prior))
    pool = current + prior
    n_current = len(current)
    n_total = len(pool)

    at_least_as_extreme = 1  # include the observed case to keep p > 0
    for _ in range(rounds):
        shuffled = pool[:]
        rng.shuffle(shuffled)
        resampled_current = shuffled[:n_current]
        resampled_prior = shuffled[n_current:n_total]
        delta = abs(stat(resampled_current) - stat(resampled_prior))
        if delta >= observed:
            at_least_as_extreme += 1
    return at_least_as_extreme / (rounds + 1)


def _refusal_values(samples: list[Sample]) -> list[float]:
    return [1.0 if classify_refusal(s.text).is_refusal else 0.0 for s in samples]


def _hedge_values(samples: list[Sample]) -> list[float]:
    return [hedge_density(s.text) for s in samples]


def _length_values(samples: list[Sample]) -> list[float]:
    return [float(len(s.text.split())) for s in samples]


def refusal_p_value(
    current: list[Sample],
    prior: list[Sample],
    *,
    rounds: int = 1000,
    rng: random.Random | None = None,
) -> float | None:
    """Permutation p-value for a shift in refusal rate."""
    return permutation_two_sample_pvalue(
        _refusal_values(current),
        _refusal_values(prior),
        stat=statistics.mean,
        rounds=rounds,
        rng=rng,
    )


def hedge_p_value(
    current: list[Sample],
    prior: list[Sample],
    *,
    rounds: int = 1000,
    rng: random.Random | None = None,
) -> float | None:
    """Permutation p-value for a shift in per-sample hedging density."""
    return permutation_two_sample_pvalue(
        _hedge_values(current),
        _hedge_values(prior),
        stat=statistics.mean,
        rounds=rounds,
        rng=rng,
    )


def length_p_value(
    current: list[Sample],
    prior: list[Sample],
    *,
    rounds: int = 1000,
    rng: random.Random | None = None,
) -> float | None:
    """Permutation p-value for a shift in response length (median words)."""
    return permutation_two_sample_pvalue(
        _length_values(current),
        _length_values(prior),
        stat=statistics.median,
        rounds=rounds,
        rng=rng,
    )
