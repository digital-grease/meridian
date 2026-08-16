"""Pre-flight cost estimation.

Pricing is published in dollars per million tokens, separately for input
and output. The table is hand-maintained; sources are the provider pricing
pages and are dated in comments so an auditor can verify.

Why the estimate reads each runner's completion cap
---------------------------------------------------
Until 2026-08 this module multiplied every planned call by a flat 500
output tokens and never looked at ``max_tokens`` at all. The estimate was
therefore structurally incapable of noticing a cap change: it printed
$11.32 for the gpt-5.5 week whether the cap was 1024 or 8192.

That stopped being harmless on 2026-07-24, when gpt-5.5's per-runner cap
was raised from the shared 1024 to 8192 to fix the truncated-completion
bug (see ``meridian/config.yaml`` and
``site/content/reports/2026-07-24-truncated-response-correction.md``).
gpt-5.5 is reasoning-default: reasoning tokens bill against the same
completion budget as visible output, so on that class of model the cap
is what bounds spend. 2026-W31 would have been the first production run
at 8192 but was lost to the EC2 capacity outage, which makes 2026-W33
the first. Worst case there is roughly 600 calls x 8192 tokens x
$30/MM, about $147, against a $5.39 historical actual at the old cap,
and the old estimator would have printed $11.32 for both.

The model below
---------------
Expected billed output per call is::

    typical + cap_exhaustion_share * (max_tokens - typical)

where ``typical`` is what a response runs to when the cap is not the
binding constraint, clamped to the cap. The second term is the tail: the
share of requests that spend the whole budget, which is the only part
that scales with the cap. Two shares are used because a reasoning-default
model plausibly runs to a large cap and a non-reasoning one does not.

Calibration receipts, from ``data/run_log.jsonl`` and the two corrections:
  - gpt-5.5 at cap 1024 (2026-W27, 2026-W29): $5.39 actual over 600
    calls, about 296 billed output tokens per call, with 47/600 and
    43/600 samples returning ``finish_reason="length"``.
  - claude-opus-4-8 at cap 1024 (2026-W28): $7.08 actual over 600
    calls, about 468 billed output tokens per call.

The estimate is deliberately a little conservative against those actuals.
It gates ``--max-cost`` in the CLI, so erring low is the failure that
costs money.

Why the estimate reads the temperature plan
-------------------------------------------
Those receipts say 600 calls, but 30 prompts x 25 samples is 750. The
difference is the zero-temperature batch: the orchestrator asks each
runner ``supports_temperature()`` before launching a batch and skips it
entirely when the model rejects that value, and both paid models in the
current roster reject ``temperature=0`` (gpt-5.5 and claude-opus-4-8
accept only the API default of 1.0). Until 2026-08 this module priced
all 750 regardless, a flat 25% over-count that made the pre-flight gate
about 25% tighter than the in-run ledger even though both take the same
``--max-cost``, so a corpus growth could abort a run whose real spend
would have been comfortably inside the ceiling. Passing a
:class:`TemperaturePlan` prices the calls that will actually be issued.
Runners that do not expose ``supports_temperature`` are assumed to
accept every value, which matches :class:`~meridian.runners.base.Runner`'s
permissive default and errs high.

Models the table cannot price
------------------------------
``PRICING`` is exact-match apart from the self-hosted wildcard, so any
model not listed is unpriceable rather than free. Both cost paths report
that rather than booking $0.00: the estimate exposes it on
:attr:`CostEstimate.unpriced` (which the CLI refuses to run under a
``--max-cost`` ceiling, since a ceiling that cannot see a runner is not
a ceiling), and :func:`meridian.sampling.cost.sample_cost_usd` returns
``None`` so the in-run ledger counts it under ``calls_unpriced``. Adding
a roster model from the CLAUDE.md roadmap (Google, xAI, DeepSeek,
Mistral) or a point release such as ``gpt-5.6`` therefore surfaces as a
loud stop instead of silently disarming both gates.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# (provider, model_id) -> (input_usd_per_mm, output_usd_per_mm).
# A "*" model_id is a wildcard for any model under that provider (used when
# a provider's whole family is priced the same, e.g. self-hosted Ollama).
# Values checked 2026-06 from each provider's pricing page / model docs.
PRICING: dict[tuple[str, str], tuple[float, float]] = {
    ("anthropic", "claude-opus-4-8"):          ( 5.00, 25.00),
    ("anthropic", "claude-opus-4-7"):          ( 5.00, 25.00),
    ("anthropic", "claude-sonnet-4-6"):        ( 3.00, 15.00),
    ("anthropic", "claude-haiku-4-5-20251001"):( 0.80,  4.00),
    ("openai",    "gpt-5.5"):                  ( 5.00, 30.00),
    ("openai",    "gpt-5.1"):                  (10.00, 30.00),
    ("openai",    "gpt-4o"):                   ( 2.50, 10.00),
    ("openai",    "gpt-4.1-mini"):             ( 0.15,  0.60),
    ("ollama",    "*"):                        ( 0.00,  0.00),  # local, free
}

#: Providers that bill nothing per token because we host the weights
#: ourselves. A model under one of these that is missing from
#: ``PRICING`` is genuinely $0.00, not unpriceable, so the cost paths
#: must not treat it as a blind spot. Everything else that misses the
#: table is a model somebody is being billed for at a rate we do not
#: know.
FREE_PROVIDERS: frozenset[str] = frozenset({"ollama"})

#: Model-id prefixes, per provider, whose models reason by default and so
#: bill reasoning tokens against the completion cap.
#:
#: This mirrors ``_TEMPERATURE_DEPRECATED_PREFIXES`` in
#: ``meridian/runners/anthropic.py`` and the GPT-5 / o-series prefixes in
#: ``meridian/runners/openai.py``, which is not an accident: the same
#: models that deprecate ``temperature`` are the ones that reason by
#: default. Those lists are not imported here because importing either
#: runner module pulls in a provider SDK, and this module must stay
#: importable as pure arithmetic (the ``estimate`` subcommand runs
#: without credentials).
REASONING_DEFAULT_PREFIXES: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-opus-4-7", "claude-opus-4-8"),
    "openai": ("gpt-5", "o1", "o3", "o4"),
}

#: Output tokens a response runs to when the completion cap is not the
#: binding constraint. Unchanged from the flat assumption this module
#: carried before it read caps at all, so estimates at the shared 1024
#: cap stay comparable with the tables in ``meridian/BUDGET.md``.
TYPICAL_OUTPUT_TOKENS = 500

#: Share of requests expected to spend the entire completion budget on a
#: reasoning-default model. Measured, not guessed: at the 1024 cap
#: gpt-5.5 returned ``finish_reason="length"`` on 47/600 samples in
#: 2026-W27 (7.8%) and 43/600 in 2026-W29 (7.2%). 0.10 rounds that up.
#: This is the term that makes the estimate move when a cap moves.
CAP_EXHAUSTION_SHARE_REASONING = 0.10

#: Same share for a model that does not reason by default. Such a model
#: stops when the answer is finished, so a larger cap buys headroom
#: rather than spend. 0.02 is an assumption rather than a measurement:
#: every paid runner in the current roster is reasoning-default, so
#: there is no in-project observation to fit it to. It is non-zero so
#: the estimate still responds to a cap change on those models.
CAP_EXHAUSTION_SHARE_STANDARD = 0.02


@dataclass(frozen=True)
class TemperaturePlan:
    """The temperatures and batch sizes a run will actually request.

    Mirrors the four ``sampling.*`` fields the orchestrator reads. Passed
    to :func:`estimate_cost` so the estimate can drop a batch the
    orchestrator will skip, which it does for any model that rejects that
    temperature at the API. Optional: omit it and every runner is priced
    for the full ``samples_per_pair``, the pre-2026-08 behaviour.
    """

    n_default_temp: int
    default_temperature: float
    n_zero_temp: int
    zero_temperature: float


@dataclass(frozen=True)
class CostEstimate:
    by_runner: dict[str, float]  # "provider/model_id" -> USD
    total: float
    #: "provider/model_id" -> one-line description of what was assumed for
    #: that runner (cap, expected output tokens per call, model class).
    #: Printed with the estimate so an operator reading a ``--max-cost``
    #: abort can see which assumption drove the number.
    assumptions: dict[str, str] = field(default_factory=dict)
    #: "provider/model_id" for every runner the pricing table could not
    #: price. Booked at $0.00 in ``by_runner`` because inventing a price
    #: would be worse, which is exactly why the list is carried
    #: separately: a caller enforcing a spend ceiling has to know the
    #: ceiling cannot see these runners.
    unpriced: tuple[str, ...] = ()

    def pretty(self) -> str:
        lines = [f"Estimated cost for planned run: ${self.total:.2f}"]
        for k, v in sorted(self.by_runner.items()):
            note = self.assumptions.get(k)
            suffix = f"  [{note}]" if note else ""
            lines.append(f"  {k}: ${v:.2f}{suffix}")
        return "\n".join(lines)


def _price_for(provider: str, model_id: str) -> tuple[float, float] | None:
    return PRICING.get((provider, model_id)) or PRICING.get((provider, "*"))


def is_priceable(provider: str, model_id: str) -> bool:
    """Can this model's spend be computed at all?

    False means the pricing table has no entry and the provider is not
    one we host ourselves, so every dollar it bills is invisible to both
    the pre-flight estimate and the in-run ledger.
    """
    if _price_for(provider, model_id) is not None:
        return True
    return provider.lower() in FREE_PROVIDERS


def calls_per_pair(runner, samples_per_pair: int, plan: TemperaturePlan | None) -> int:
    """How many requests this runner will really make per (prompt, model).

    The orchestrator skips a whole batch when
    ``runner.supports_temperature()`` says the model rejects that value,
    so pricing ``samples_per_pair`` unconditionally over-counts every
    temperature-restricted model. Without a plan, or for an object with
    no ``supports_temperature`` (the estimator is duck-typed and prices
    config specs as well as live runners), the answer is the full
    ``samples_per_pair``: that matches
    :class:`~meridian.runners.base.Runner`'s permissive default and errs
    high, which is the safe direction for a spend gate.
    """
    if plan is None:
        return samples_per_pair
    supports = getattr(runner, "supports_temperature", None)
    if not callable(supports):
        return samples_per_pair
    calls = 0
    if supports(plan.default_temperature):
        calls += plan.n_default_temp
    if supports(plan.zero_temperature):
        calls += plan.n_zero_temp
    return calls


def is_reasoning_default(provider: str, model_id: str) -> bool:
    """Does this model bill reasoning tokens against the completion cap?

    Prefix match, so a point release (``gpt-5.6``, ``claude-opus-4-9``)
    inherits the classification instead of silently falling back to the
    cheaper assumption on the first week it ships.
    """
    prefixes = REASONING_DEFAULT_PREFIXES.get(provider.lower(), ())
    mid = model_id.lower()
    return any(mid.startswith(p) for p in prefixes)


def expected_output_tokens(provider: str, model_id: str, max_tokens: int) -> float:
    """Billed output tokens to assume for one call at this cap.

    Never exceeds ``max_tokens``: a cap set below the typical response
    length simply truncates, which is the 2026-W27 failure mode and is
    priced as such (the tokens are billed either way).
    """
    typical = min(TYPICAL_OUTPUT_TOKENS, max_tokens)
    share = (
        CAP_EXHAUSTION_SHARE_REASONING
        if is_reasoning_default(provider, model_id)
        else CAP_EXHAUSTION_SHARE_STANDARD
    )
    return typical + share * (max_tokens - typical)


def max_tokens_for(runner, default_max_tokens: int) -> int:
    """Resolve the completion cap that will actually be sent for ``runner``.

    Mirrors the orchestrator's resolution order (``runner.max_tokens_override
    or plan.max_tokens``) so the estimate prices the request the run will
    really make. ``max_tokens`` is accepted as a second source so a
    :class:`~meridian.config.RunnerSpec`, which spells the field that way,
    can be priced directly without building an SDK-backed runner.
    """
    cap = getattr(runner, "max_tokens_override", None)
    if cap is None:
        cap = getattr(runner, "max_tokens", None)
    if cap is None:
        return default_max_tokens
    return int(cap)


def estimate_cost(
    runners: list,  # list[Runner] but avoid circular import
    n_prompts: int,
    samples_per_pair: int,
    *,
    default_max_tokens: int,
    temperature_plan: TemperaturePlan | None = None,
    avg_input_chars: int = 80,
) -> CostEstimate:
    """Price a planned run.

    ``default_max_tokens`` is the shared ``sampling.max_tokens`` from
    config; per-runner overrides win, exactly as they do at sample time.
    It is required rather than defaulted: a default here would quietly
    duplicate :class:`~meridian.config.SamplingSpec`'s, so a future call
    site that forgot the argument would reintroduce the cap-blindness
    this module was rewritten to remove, with no test failing.

    ``temperature_plan`` lets the estimate skip a batch the orchestrator
    will skip. Without it every runner is priced for the full
    ``samples_per_pair``, which over-counts any temperature-restricted
    model.
    """
    by_runner: dict[str, float] = {}
    assumptions: dict[str, str] = {}
    unpriced: list[str] = []
    for r in runners:
        key = f"{r.provider}/{r.model_id}"
        cap = max_tokens_for(r, default_max_tokens)
        per_pair = calls_per_pair(r, samples_per_pair, temperature_plan)
        pricing = _price_for(r.provider, r.model_id)
        if pricing is None:
            by_runner[key] = 0.0
            assumptions[key] = "no price on file, counted as $0.00"
            if not is_priceable(r.provider, r.model_id):
                unpriced.append(key)
            continue
        in_usd, out_usd = pricing
        # Rough token count from characters. English averages ~4 chars/token.
        total_calls = n_prompts * per_pair
        out_per_call = expected_output_tokens(r.provider, r.model_id, cap)
        in_tok = total_calls * (avg_input_chars / 4)
        out_tok = total_calls * out_per_call
        cost = (in_tok / 1_000_000) * in_usd + (out_tok / 1_000_000) * out_usd
        by_runner[key] = round(cost, 2)
        note = (
            f"max_tokens={cap}, {total_calls} call(s), "
            f"~{out_per_call:.0f} output tok/call, "
            + ("reasoning-default"
               if is_reasoning_default(r.provider, r.model_id)
               else "standard")
        )
        if per_pair < samples_per_pair:
            note += f" ({samples_per_pair - per_pair} sample(s)/pair skipped: "
            note += "model rejects that temperature)"
        assumptions[key] = note
    # Sum the rounded per-runner figures rather than the raw ones, so the
    # lines an operator reads in a --max-cost abort add up to the total
    # that triggered it. The difference is at most a cent per runner.
    return CostEstimate(
        by_runner=by_runner,
        total=round(sum(by_runner.values()), 2),
        assumptions=assumptions,
        unpriced=tuple(unpriced),
    )
