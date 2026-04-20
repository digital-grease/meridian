"""Benjamini-Hochberg multiple-testing correction.

With hundreds of (prompt × model × week) comparisons reported weekly, the
expected number of false positives under a 0.05 significance threshold is
guaranteed to be nonzero even when nothing is actually drifting. BH
controls the expected *false discovery rate* — the share of rejections
that are false — rather than family-wise error, which is the right target
when we want to surface many true discoveries and can tolerate a small
fraction of false ones.

Usage:
    decisions = bh_correct([
        ("pair-1", 0.001),
        ("pair-2", 0.04),
        ("pair-3", 0.5),
    ], fdr=0.05)
    # decisions[i].rejected is True iff the null should be rejected
    # at the configured FDR.

Input p-values come from whatever significance test the caller used
(two-proportion z-test on refusal rates, bootstrap tail probability on
hedge shifts, etc.). This module doesn't compute p-values; it only
classifies them.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TestResult:
    test_id: str
    p_value: float
    adjusted_p_value: float
    rejected: bool


def bh_correct(
    tests: list[tuple[str, float]],
    *,
    fdr: float = 0.05,
) -> list[TestResult]:
    """Return BH-adjusted test results ordered to match the input.

    ``tests`` is a list of ``(test_id, p_value)`` pairs. Callers can then
    render ``rejected`` in the UI and use ``adjusted_p_value`` for sorting.
    """
    if not tests:
        return []
    if not 0.0 < fdr < 1.0:
        raise ValueError(f"fdr must be in (0, 1), got {fdr}")

    n = len(tests)
    # Sort by p-value ascending, keeping original position so we can restore order.
    indexed = sorted(enumerate(tests), key=lambda kv: kv[1][1])

    # Compute adjusted p-values using the standard BH monotone form:
    #   q_i = min_{j >= i} ( n * p_(j) / j )
    # then clipped to [0, 1].
    ordered_adjusted: list[float] = [0.0] * n
    running_min = 1.0
    for rank_from_top in range(n, 0, -1):
        _, (_, p) = indexed[rank_from_top - 1]
        raw = (n * p) / rank_from_top
        running_min = min(running_min, raw)
        ordered_adjusted[rank_from_top - 1] = max(0.0, min(1.0, running_min))

    # Largest rank with p_(k) <= (k/n) * fdr -> reject all 1..k.
    k_star = 0
    for rank_from_top in range(1, n + 1):
        _, (_, p) = indexed[rank_from_top - 1]
        if p <= (rank_from_top / n) * fdr:
            k_star = rank_from_top

    # Assemble results in original input order.
    results: list[TestResult | None] = [None] * n
    for rank_from_top in range(1, n + 1):
        orig_idx, (test_id, p) = indexed[rank_from_top - 1]
        results[orig_idx] = TestResult(
            test_id=test_id,
            p_value=p,
            adjusted_p_value=round(ordered_adjusted[rank_from_top - 1], 6),
            rejected=rank_from_top <= k_star,
        )
    return [r for r in results if r is not None]
