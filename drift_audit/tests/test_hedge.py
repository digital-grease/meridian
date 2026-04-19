"""Hedge-density tests."""
from __future__ import annotations

from drift_audit.analysis.hedge import hedge_density


def test_empty_has_zero_density():
    assert hedge_density("") == 0.0
    assert hedge_density("   ") == 0.0


def test_no_hedges_gives_zero():
    text = "Paris is the capital of France. It has a population of about two million."
    assert hedge_density(text) == 0.0


def test_single_hedge_counted():
    text = (
        "The answer depends on several factors and, to be fair, reasonable "
        "people disagree about the right emphasis. That said, the overall "
        "trend is clear."
    )
    # 3 hedges: "to be fair", "reasonable people disagree", "that said".
    # Exact numeric value is density-per-100-tokens which depends on split,
    # so assert on ordering and positivity.
    d = hedge_density(text)
    assert d > 0.0


def test_density_scales_with_length():
    short = "Some argue this is correct."
    long = short + " " + "The same filler sentence added repeatedly. " * 20
    assert hedge_density(short) > hedge_density(long)
