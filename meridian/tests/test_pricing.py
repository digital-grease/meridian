"""Cost estimator tests."""
from __future__ import annotations

from meridian.sampling.pricing import estimate_cost


class _R:
    """Minimal duck-type standing in for a Runner for pricing purposes."""
    def __init__(self, provider: str, model_id: str):
        self.provider = provider
        self.model_id = model_id


def test_ollama_costs_zero():
    est = estimate_cost([_R("ollama", "llama3.2:3b")], n_prompts=20, samples_per_pair=25)
    assert est.by_runner["ollama/llama3.2:3b"] == 0.0
    assert est.total == 0.0


def test_anthropic_opus_nonzero():
    est = estimate_cost([_R("anthropic", "claude-opus-4-8")], n_prompts=20, samples_per_pair=25)
    assert est.by_runner["anthropic/claude-opus-4-8"] > 0.0
    assert est.total == est.by_runner["anthropic/claude-opus-4-8"]


def test_unknown_model_falls_back_to_zero():
    est = estimate_cost([_R("openai", "definitely-not-real-model")], n_prompts=20, samples_per_pair=25)
    assert est.by_runner["openai/definitely-not-real-model"] == 0.0


def test_multiple_runners_sum_correctly():
    runners = [
        _R("anthropic", "claude-opus-4-8"),
        _R("openai", "gpt-4o"),
        _R("ollama", "llama3.2:3b"),
    ]
    est = estimate_cost(runners, n_prompts=20, samples_per_pair=25)
    assert est.total == round(sum(est.by_runner.values()), 2)
    assert est.by_runner["ollama/llama3.2:3b"] == 0.0
    assert est.by_runner["anthropic/claude-opus-4-8"] > est.by_runner["openai/gpt-4o"]
