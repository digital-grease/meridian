"""Cost estimator tests.

The cap-aware cases exist because the estimator used to be structurally
incapable of noticing a completion-cap change: it multiplied every call
by a flat 500 output tokens, so it printed the same ~$11.32 for the
gpt-5.5 week whether the cap was 1024 or 8192. That number then feeds
``run --max-cost``, which is the only thing standing between an
unattended run and the ~$147 worst case on the 8192 cap.
"""
from __future__ import annotations

import pytest

from meridian.sampling.pricing import (
    CAP_EXHAUSTION_SHARE_REASONING,
    TYPICAL_OUTPUT_TOKENS,
    TemperaturePlan,
    calls_per_pair,
    estimate_cost,
    expected_output_tokens,
    is_priceable,
    is_reasoning_default,
    max_tokens_for,
)


class _R:
    """Minimal duck-type standing in for a Runner for pricing purposes.

    ``max_tokens_override`` is the name :class:`~meridian.runners.base.Runner`
    uses, which is what the estimator reads.

    ``supports_temperature`` is deliberately absent unless a test asks for
    one, so the no-plan path stays exercised. ``default_max_tokens`` is
    always passed explicitly below: it is a required argument precisely
    so that a call site cannot forget the cap, and spelling it out
    documents which cap each assertion is about.
    """
    def __init__(self, provider: str, model_id: str, max_tokens_override: int | None = None):
        self.provider = provider
        self.model_id = model_id
        self.max_tokens_override = max_tokens_override


class _TempRestrictedR(_R):
    """A runner that rejects every temperature except the API default.

    The shape of both paid models in the current roster: gpt-5.5 and
    claude-opus-4-8 accept 1.0 only, so the orchestrator never sends
    their zero-temp batch.
    """
    def supports_temperature(self, temperature: float) -> bool:
        return temperature == 1.0


def test_ollama_costs_zero():
    est = estimate_cost(
        [_R("ollama", "llama3.2:3b")], n_prompts=20, samples_per_pair=25,
        default_max_tokens=1024,
    )
    assert est.by_runner["ollama/llama3.2:3b"] == 0.0
    assert est.total == 0.0


def test_anthropic_opus_nonzero():
    est = estimate_cost(
        [_R("anthropic", "claude-opus-4-8")], n_prompts=20, samples_per_pair=25,
        default_max_tokens=1024,
    )
    assert est.by_runner["anthropic/claude-opus-4-8"] > 0.0
    assert est.total == est.by_runner["anthropic/claude-opus-4-8"]


def test_unknown_model_falls_back_to_zero_and_is_reported_unpriced():
    """Booking $0.00 is the least-wrong number to print, but it also
    means a --max-cost ceiling cannot see this runner at all, so the
    estimate has to say so out of band."""
    est = estimate_cost(
        [_R("openai", "definitely-not-real-model")], n_prompts=20,
        samples_per_pair=25, default_max_tokens=1024,
    )
    assert est.by_runner["openai/definitely-not-real-model"] == 0.0
    assert est.unpriced == ("openai/definitely-not-real-model",)


def test_self_hosted_models_are_free_not_unpriced():
    """The local control group is genuinely $0.00. Flagging it as
    unpriceable would block every ceiling-guarded run, since ollama runs
    every week by design."""
    est = estimate_cost(
        [_R("ollama", "some-model-never-listed")], n_prompts=20,
        samples_per_pair=25, default_max_tokens=1024,
    )
    assert est.unpriced == ()
    assert is_priceable("ollama", "some-model-never-listed")
    assert not is_priceable("google", "gemini-3-pro")
    assert is_priceable("openai", "gpt-5.5")


def test_multiple_runners_sum_correctly():
    runners = [
        _R("anthropic", "claude-opus-4-8"),
        _R("openai", "gpt-4o"),
        _R("ollama", "llama3.2:3b"),
    ]
    est = estimate_cost(
        runners, n_prompts=20, samples_per_pair=25, default_max_tokens=1024,
    )
    assert est.total == round(sum(est.by_runner.values()), 2)
    assert est.by_runner["ollama/llama3.2:3b"] == 0.0
    assert est.by_runner["anthropic/claude-opus-4-8"] > est.by_runner["openai/gpt-4o"]


# --- reasoning-default classification ---


def test_reasoning_default_classification():
    assert is_reasoning_default("openai", "gpt-5.5")
    assert is_reasoning_default("openai", "gpt-5.1")
    assert is_reasoning_default("anthropic", "claude-opus-4-8")
    assert not is_reasoning_default("openai", "gpt-4o")
    assert not is_reasoning_default("anthropic", "claude-haiku-4-5-20251001")
    assert not is_reasoning_default("ollama", "llama3.2:3b")


def test_reasoning_classification_is_prefix_matched():
    """A point release must inherit the classification rather than
    silently fall back to the cheaper assumption on release week."""
    assert is_reasoning_default("openai", "gpt-5.9-preview")
    assert is_reasoning_default("anthropic", "claude-opus-4-8-20260801")


# --- expected output tokens ---


def test_expected_output_tokens_never_exceeds_cap():
    for cap in (16, 64, 500, 1024, 8192):
        for provider, model in (("openai", "gpt-5.5"), ("openai", "gpt-4o")):
            assert expected_output_tokens(provider, model, cap) <= cap


def test_expected_output_tokens_clamps_to_a_tiny_cap():
    """A cap below the typical response length simply truncates, and the
    tokens are billed either way, so the whole cap is the estimate."""
    assert expected_output_tokens("openai", "gpt-5.5", 64) == 64


def test_reasoning_model_uses_more_of_a_large_cap():
    reasoning = expected_output_tokens("openai", "gpt-5.5", 8192)
    standard = expected_output_tokens("openai", "gpt-4o", 8192)
    assert reasoning > standard
    # The reasoning figure is the documented model: typical + measured
    # cap-exhaustion share of the headroom above it.
    assert reasoning == (
        TYPICAL_OUTPUT_TOKENS
        + CAP_EXHAUSTION_SHARE_REASONING * (8192 - TYPICAL_OUTPUT_TOKENS)
    )


# --- cap resolution ---


def test_max_tokens_for_prefers_runner_override():
    """Mirrors the orchestrator's `runner.max_tokens_override or
    plan.max_tokens` resolution, so the estimate prices the request the
    run will actually make."""
    assert max_tokens_for(_R("openai", "gpt-5.5", 8192), 1024) == 8192
    assert max_tokens_for(_R("openai", "gpt-5.5"), 1024) == 1024


def test_max_tokens_for_accepts_a_runner_spec():
    """RunnerSpec spells the field `max_tokens`; the estimate subcommand
    prices specs directly rather than building SDK-backed runners."""
    from meridian.config import RunnerSpec

    spec = RunnerSpec(provider="openai", model_id="gpt-5.5", max_tokens=8192)
    assert max_tokens_for(spec, 1024) == 8192
    bare = RunnerSpec(provider="openai", model_id="gpt-4o")
    assert max_tokens_for(bare, 1024) == 1024


# --- the estimate moves with the cap ---


def test_estimate_scales_with_runner_max_tokens():
    """The regression this file exists for: raising gpt-5.5's cap from
    1024 to 8192 must move the printed estimate."""
    at_1024 = estimate_cost(
        [_R("openai", "gpt-5.5", 1024)], n_prompts=30, samples_per_pair=25,
        default_max_tokens=1024,
    )
    at_8192 = estimate_cost(
        [_R("openai", "gpt-5.5", 8192)], n_prompts=30, samples_per_pair=25,
        default_max_tokens=1024,
    )
    assert at_8192.total > at_1024.total * 2, (
        f"estimate barely moved: ${at_1024.total} -> ${at_8192.total}"
    )
    # And it stays well under the arithmetic worst case (every call
    # spending the whole cap), which is what makes it an estimate rather
    # than an alarm: 750 calls x 8192 tokens x $30/MM = $184.
    assert at_8192.total < 184.0


def test_estimate_honours_default_max_tokens_when_no_override():
    """The shared sampling.max_tokens is the baseline for runners that
    do not pin their own cap."""
    low = estimate_cost(
        [_R("openai", "gpt-5.5")], n_prompts=30, samples_per_pair=25,
        default_max_tokens=1024,
    )
    high = estimate_cost(
        [_R("openai", "gpt-5.5")], n_prompts=30, samples_per_pair=25,
        default_max_tokens=8192,
    )
    assert high.total > low.total


def test_runner_override_beats_default_max_tokens():
    pinned = estimate_cost(
        [_R("openai", "gpt-5.5", 8192)], n_prompts=30, samples_per_pair=25,
        default_max_tokens=1024,
    )
    unpinned = estimate_cost(
        [_R("openai", "gpt-5.5")], n_prompts=30, samples_per_pair=25,
        default_max_tokens=1024,
    )
    assert pinned.total > unpinned.total


def test_standard_model_barely_moves_with_a_bigger_cap():
    """A non-reasoning model stops when the answer is done, so a bigger
    cap buys headroom rather than spend."""
    at_1024 = estimate_cost(
        [_R("openai", "gpt-4o", 1024)], n_prompts=30, samples_per_pair=25,
        default_max_tokens=1024,
    )
    at_8192 = estimate_cost(
        [_R("openai", "gpt-4o", 8192)], n_prompts=30, samples_per_pair=25,
        default_max_tokens=1024,
    )
    assert at_8192.total < at_1024.total * 1.5


def test_estimate_at_the_shared_cap_matches_the_historical_number():
    """Backstop against re-tuning the model into a different regime.

    At the shared 1024 cap the estimate must stay close to what the old
    flat-500-tokens estimator produced, so the tables in
    ``meridian/BUDGET.md`` and the run log's historical
    ``estimated_cost_usd`` column remain comparable.
    """
    est = estimate_cost(
        [_R("anthropic", "claude-opus-4-8", 1024)], n_prompts=30, samples_per_pair=25,
        default_max_tokens=1024,
    )
    old_flat_500 = 30 * 25 * (500 * 25.0 / 1_000_000 + (80 / 4) * 5.0 / 1_000_000)
    assert 0.85 * old_flat_500 < est.total < 1.25 * old_flat_500


# --- the temperature plan ---


_ROSTER_PLAN = TemperaturePlan(
    n_default_temp=20, default_temperature=1.0,
    n_zero_temp=5, zero_temperature=0.0,
)


def test_calls_per_pair_drops_a_batch_the_run_will_skip():
    """The orchestrator asks supports_temperature() before launching each
    batch. gpt-5.5 and claude-opus-4-8 both reject temperature=0, which
    is why the 2026-W27/W29 receipts say 600 calls and not 750."""
    assert calls_per_pair(_TempRestrictedR("openai", "gpt-5.5"), 25, _ROSTER_PLAN) == 20
    assert calls_per_pair(_R("openai", "gpt-4o"), 25, _ROSTER_PLAN) == 25


def test_calls_per_pair_is_permissive_without_a_plan_or_a_method():
    """Over-counting is the safe direction for a spend gate, and it
    matches Runner.supports_temperature's permissive default."""
    assert calls_per_pair(_TempRestrictedR("openai", "gpt-5.5"), 25, None) == 25
    assert calls_per_pair(_R("openai", "gpt-5.5"), 25, _ROSTER_PLAN) == 25


def test_temperature_plan_removes_the_zero_temp_over_count():
    """The pre-flight gate and the in-run ledger take the same
    --max-cost number, so the estimate pricing 25% more calls than the
    run makes leaves the pre-flight gate 25% tighter than the ledger."""
    runner = _TempRestrictedR("openai", "gpt-5.5", 8192)
    without = estimate_cost(
        [runner], n_prompts=30, samples_per_pair=25, default_max_tokens=8192,
    )
    with_plan = estimate_cost(
        [runner], n_prompts=30, samples_per_pair=25, default_max_tokens=8192,
        temperature_plan=_ROSTER_PLAN,
    )
    # 600 calls priced instead of 750.
    assert with_plan.total == pytest.approx(without.total * 600 / 750, rel=0.01)
    assert "600 call(s)" in with_plan.assumptions["openai/gpt-5.5"]
    assert "5 sample(s)/pair skipped" in with_plan.assumptions["openai/gpt-5.5"]


def test_temperature_plan_leaves_unrestricted_models_alone():
    runner = _R("openai", "gpt-4o", 1024)
    without = estimate_cost(
        [runner], n_prompts=30, samples_per_pair=25, default_max_tokens=1024,
    )
    with_plan = estimate_cost(
        [runner], n_prompts=30, samples_per_pair=25, default_max_tokens=1024,
        temperature_plan=_ROSTER_PLAN,
    )
    assert with_plan.total == without.total


def test_estimate_scales_with_prompt_count():
    """Held-out prompts are sampled too, so a corpus with a held-out set
    must price strictly higher than its public subset. `run` prices
    corpus.all() for exactly this reason: pricing public() once the
    CLAUDE.md 30% split lands would under-count the run and let a
    --max-cost ceiling pass work it should have blocked."""
    runners = [_TempRestrictedR("anthropic", "claude-opus-4-8", 1024)]
    public_only = estimate_cost(
        runners, n_prompts=30, samples_per_pair=25, default_max_tokens=1024,
        temperature_plan=_ROSTER_PLAN,
    )
    with_held_out = estimate_cost(
        runners, n_prompts=43, samples_per_pair=25, default_max_tokens=1024,
        temperature_plan=_ROSTER_PLAN,
    )
    assert with_held_out.total > public_only.total


# --- reporting ---


def test_assumptions_are_reported_per_runner():
    """A --max-cost abort prints these, so an operator can see which
    assumption drove the number that blocked the run."""
    est = estimate_cost(
        [_R("openai", "gpt-5.5", 8192), _R("openai", "totally-unknown")],
        n_prompts=30, samples_per_pair=25, default_max_tokens=1024,
    )
    note = est.assumptions["openai/gpt-5.5"]
    assert "max_tokens=8192" in note
    assert "reasoning-default" in note
    assert "no price on file" in est.assumptions["openai/totally-unknown"]
    assert "max_tokens=8192" in est.pretty()
