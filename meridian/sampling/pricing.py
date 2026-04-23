"""Pre-flight cost estimation.

Pricing is published in dollars per million tokens, separately for input
and output. The table is hand-maintained; sources are the provider pricing
pages and are dated in comments so an auditor can verify.
"""
from __future__ import annotations

from dataclasses import dataclass

# (provider, model_id) -> (input_usd_per_mm, output_usd_per_mm).
# A "*" model_id is a wildcard for any model under that provider (used when
# a provider's whole family is priced the same, e.g. self-hosted Ollama).
# Values checked 2026-04 from each provider's pricing page.
PRICING: dict[tuple[str, str], tuple[float, float]] = {
    ("anthropic", "claude-opus-4-7"):          (15.00, 75.00),
    ("anthropic", "claude-sonnet-4-6"):        ( 3.00, 15.00),
    ("anthropic", "claude-haiku-4-5-20251001"):( 0.80,  4.00),
    ("openai",    "gpt-5-preview"):            (10.00, 30.00),  # placeholder
    ("openai",    "gpt-4o"):                   ( 2.50, 10.00),
    ("openai",    "gpt-4.1-mini"):             ( 0.15,  0.60),
    ("ollama",    "*"):                        ( 0.00,  0.00),  # local, free
}


@dataclass(frozen=True)
class CostEstimate:
    by_runner: dict[str, float]  # "provider/model_id" -> USD
    total: float

    def pretty(self) -> str:
        lines = [f"Estimated cost for planned run: ${self.total:.2f}"]
        for k, v in sorted(self.by_runner.items()):
            lines.append(f"  {k}: ${v:.2f}")
        return "\n".join(lines)


def _price_for(provider: str, model_id: str) -> tuple[float, float] | None:
    return PRICING.get((provider, model_id)) or PRICING.get((provider, "*"))


def estimate_cost(
    runners: list,  # list[Runner] but avoid circular import
    n_prompts: int,
    samples_per_pair: int,
    *,
    avg_input_chars: int = 80,
    avg_output_tokens: int = 500,
) -> CostEstimate:
    by_runner: dict[str, float] = {}
    total = 0.0
    for r in runners:
        key = f"{r.provider}/{r.model_id}"
        pricing = _price_for(r.provider, r.model_id)
        if pricing is None:
            by_runner[key] = 0.0
            continue
        in_usd, out_usd = pricing
        # Rough token count from characters. English averages ~4 chars/token.
        total_calls = n_prompts * samples_per_pair
        in_tok = total_calls * (avg_input_chars / 4)
        out_tok = total_calls * avg_output_tokens
        cost = (in_tok / 1_000_000) * in_usd + (out_tok / 1_000_000) * out_usd
        by_runner[key] = round(cost, 2)
        total += cost
    return CostEstimate(by_runner=by_runner, total=round(total, 2))
