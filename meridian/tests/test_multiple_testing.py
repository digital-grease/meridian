"""Benjamini-Hochberg correction tests."""
from __future__ import annotations

import pytest

from meridian.analysis.multiple_testing import bh_correct


def test_empty_input():
    assert bh_correct([]) == []


def test_all_small_p_values_rejected():
    tests = [(f"t{i}", 0.001) for i in range(5)]
    results = bh_correct(tests, fdr=0.05)
    assert all(r.rejected for r in results)


def test_all_large_p_values_not_rejected():
    tests = [(f"t{i}", 0.9) for i in range(5)]
    results = bh_correct(tests, fdr=0.05)
    assert not any(r.rejected for r in results)


def test_bh_is_more_permissive_than_bonferroni():
    # 10 tests with a gradient of real signal. Bonferroni threshold
    # at alpha=0.05 is p < 0.005, rejecting only one. BH at fdr=0.05
    # should reject four.
    p_values = [0.003, 0.006, 0.012, 0.020, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
    tests = [(f"t{i}", p) for i, p in enumerate(p_values)]
    results = bh_correct(tests, fdr=0.05)
    n_bh_rejected = sum(1 for r in results if r.rejected)
    n_bonferroni_rejected = sum(1 for _, p in tests if p < 0.005)
    assert n_bh_rejected > n_bonferroni_rejected
    assert n_bh_rejected == 4


def test_adjusted_p_values_monotone():
    """After BH, adjusted p-values sorted ascending should be nondecreasing."""
    p_values = [0.001, 0.01, 0.05, 0.1, 0.5]
    tests = [(f"t{i}", p) for i, p in enumerate(p_values)]
    results = bh_correct(tests, fdr=0.1)
    # Results come back in input order; sort by raw p to check monotonicity.
    by_p = sorted(results, key=lambda r: r.p_value)
    adj = [r.adjusted_p_value for r in by_p]
    for a, b in zip(adj, adj[1:]):
        assert a <= b + 1e-9


def test_output_preserves_input_order():
    tests = [("c", 0.5), ("a", 0.01), ("b", 0.001)]
    results = bh_correct(tests, fdr=0.05)
    assert [r.test_id for r in results] == ["c", "a", "b"]


def test_fdr_out_of_range_rejected():
    with pytest.raises(ValueError):
        bh_correct([("t", 0.01)], fdr=0.0)
    with pytest.raises(ValueError):
        bh_correct([("t", 0.01)], fdr=1.0)
