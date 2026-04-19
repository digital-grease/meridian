"""Length summary + bootstrap CI tests."""
from __future__ import annotations

from drift_audit.analysis.confidence import bootstrap_ci
from drift_audit.analysis.length import summarize_lengths


def test_length_summary_empty():
    s = summarize_lengths([])
    assert s.n == 0
    assert s.median == 0.0


def test_length_summary_single():
    s = summarize_lengths(["hello world"])
    assert s.n == 1
    assert s.median == 2
    assert s.p25 == 2
    assert s.p75 == 2


def test_length_summary_multi():
    texts = ["a b", "a b c", "a b c d", "a", "a b c d e"]
    s = summarize_lengths(texts)
    assert s.n == 5
    assert s.median == 3.0
    assert s.p25 <= s.median <= s.p75


def test_bootstrap_ci_seeded_is_deterministic():
    observations = [0.0] * 15 + [1.0] * 5  # 25% refusal rate
    a = bootstrap_ci(observations, seed=123)
    b = bootstrap_ci(observations, seed=123)
    assert a == b


def test_bootstrap_ci_covers_true_mean():
    observations = [0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0]  # 0.5
    ci = bootstrap_ci(observations, seed=42)
    assert ci.lower <= 0.5 <= ci.upper
    assert 0.0 <= ci.lower <= ci.upper <= 1.0


def test_bootstrap_ci_degenerate_input():
    assert bootstrap_ci([]).lower == 0.0
    ci = bootstrap_ci([0.0, 0.0, 0.0], seed=1)
    assert ci.lower == ci.upper == 0.0
