"""Actual-cost tracker based on stored Sample token counts.

The pre-flight :mod:`meridian.sampling.pricing` estimator works from
average-length heuristics. This module computes the *actual* cost of a
run from the token counts each :class:`Sample` carries after capture.

Priced to the same table as the estimator; if a runner's token counts
are missing (some providers are flaky about reporting them), that
(prompt × model × week) simply contributes zero to the actual-cost total.

It also holds the live spend ceiling
------------------------------------
:class:`BudgetLedger` and :class:`BudgetGuardedRunner` turn the same
arithmetic into an in-run stop. They exist because the pre-flight
estimate is exactly the thing that has proven unreliable: it was blind
to ``max_tokens`` until 2026-08, and a run that starts under the ceiling
can still finish far above it if a model's output distribution shifts
mid-week. The ledger prices each sample as it lands and refuses to issue
further requests once the run has spent its ceiling.

The stop is deliberately implemented as "raise instead of calling the
provider", not as "unwind the run". Every sample already captured stays
in storage and the caller still writes its run-log entry: retention is
append-only and forever, so a budget stop must never cost us data we
already paid for. The orchestrator records each refused pair as a failed
pair, which is accurate, costs nothing, and makes the abort loud in both
the run log and ``check_run_health.py``. One caveat on "costs nothing":
the batch that trips the ceiling has requests already on the wire that
get cancelled and are still billed. See :class:`BudgetGuardedRunner`.
"""
from __future__ import annotations

from dataclasses import dataclass

from meridian.runners.base import Runner, RunnerError, Sample
from meridian.sampling.pricing import PRICING, is_priceable


@dataclass(frozen=True)
class CostReport:
    total_usd: float
    by_runner: dict[str, float]
    samples_priced: int
    samples_skipped_no_tokens: int


def _price_for(provider: str, model_id: str) -> tuple[float, float] | None:
    return PRICING.get((provider, model_id)) or PRICING.get((provider, "*"))


def sample_cost_usd(sample: Sample) -> float | None:
    """Cost of one captured sample, or ``None`` if it cannot be priced.

    Two things make a sample unpriceable, and both return ``None``:

    * the provider reported no token counts, so there is nothing to
      multiply a rate by;
    * the model is absent from ``PRICING`` and its provider is not one
      we host ourselves, so the rate itself is unknown.

    The second used to return 0.00 ("unknown model; treat as free"),
    which disarmed the ``--max-cost`` ceiling completely for that runner:
    the ledger charged nothing, never tripped, and the run billed real
    money against a limit that was watching an empty tally. ``PRICING``
    is exact-match apart from the self-hosted wildcard, so this fires on
    any roster addition (Google, xAI, DeepSeek, Mistral) and on any point
    release such as ``gpt-5.6`` or ``claude-opus-4-9``. Returning ``None``
    routes those into ``BudgetLedger.calls_unpriced``, which is printed
    with the spend, so the blindness is visible instead of silent.

    A genuine $0.00 (the self-hosted control group) is still 0.0, because
    a ceiling that stopped the free local baseline would defeat the
    continuous reference the silent-update detector needs.
    """
    if sample.input_tokens is None or sample.output_tokens is None:
        return None
    pricing = _price_for(sample.provider, sample.model_id)
    if pricing is None:
        return 0.0 if is_priceable(sample.provider, sample.model_id) else None
    in_usd, out_usd = pricing
    return (
        (sample.input_tokens / 1_000_000) * in_usd
        + (sample.output_tokens / 1_000_000) * out_usd
    )


def compute_actual_cost(samples: list[Sample]) -> CostReport:
    by_runner: dict[str, float] = {}
    priced = 0
    skipped = 0
    total = 0.0
    for s in samples:
        if s.input_tokens is None or s.output_tokens is None:
            skipped += 1
            continue
        pricing = _price_for(s.provider, s.model_id)
        if pricing is None:
            # Unknown model contributes nothing. Better than making up a
            # price: this is the historical-record number written to the
            # run log, and a guessed rate would be a fabricated receipt.
            # Deliberately NOT the same call as sample_cost_usd, which
            # returns None here so the live ceiling can refuse to fly
            # blind. Reporting a past week and gating future spend want
            # opposite defaults.
            priced += 1
            continue
        in_usd, out_usd = pricing
        cost = (
            (s.input_tokens / 1_000_000) * in_usd
            + (s.output_tokens / 1_000_000) * out_usd
        )
        key = f"{s.provider}/{s.model_id}"
        by_runner[key] = round(by_runner.get(key, 0.0) + cost, 6)
        total += cost
        priced += 1
    return CostReport(
        total_usd=round(total, 4),
        by_runner={k: round(v, 4) for k, v in by_runner.items()},
        samples_priced=priced,
        samples_skipped_no_tokens=skipped,
    )


class BudgetExceeded(RunnerError):
    """Raised in place of a provider call once the run has spent its ceiling.

    A :class:`~meridian.runners.base.RunnerError` on purpose: the
    orchestrator already knows how to record one of those per pair and
    carry on, so every pair after the stop fails immediately and without
    issuing a request, instead of the process dying with samples
    half-written. "Without issuing a request" is exact for those later
    pairs; the requests already in flight inside the batch that tripped
    are a separate story, described on :class:`BudgetGuardedRunner`.
    """


@dataclass
class BudgetLedger:
    """Running tally of what this process has spent, against a ceiling.

    Tracks only spend by *this invocation*. That is narrower than the
    ``actual_cost_usd`` written to the run log, which sums every sample
    stored for the week including ones a resumed earlier run paid for.
    A ceiling is about the money this process is authorised to spend, so
    resumed weeks must not consume the budget twice.

    Known blind spot: spend is only as good as the inputs to the
    arithmetic. A provider that reports no token counts, and a model with
    no row in ``PRICING``, both charge nothing here and so cannot move
    the ceiling. Those calls are counted (``calls_unpriced``) and printed
    with the spend so the blindness is visible rather than silent. The
    pre-flight estimate check is the backstop for both cases, which is
    part of why both checks exist.
    """

    ceiling_usd: float
    spent_usd: float = 0.0
    calls_charged: int = 0
    #: Calls the provider returned without usage numbers.
    calls_missing_tokens: int = 0
    #: Calls whose model has no row in ``PRICING``. Tracked apart from
    #: ``calls_missing_tokens`` because the two have different fixes: one
    #: is a provider going quiet on usage reporting, the other is a
    #: roster addition nobody added a price for.
    calls_no_price: int = 0
    #: Spend at the moment the ceiling first blocked a call. ``None``
    #: while the run is still under budget.
    tripped_at_usd: float | None = None

    @property
    def tripped(self) -> bool:
        return self.tripped_at_usd is not None

    @property
    def calls_unpriced(self) -> int:
        """Calls that could not move the ceiling, for either reason."""
        return self.calls_missing_tokens + self.calls_no_price

    @property
    def remaining_usd(self) -> float:
        return self.ceiling_usd - self.spent_usd

    def check(self) -> None:
        """Raise :class:`BudgetExceeded` if the ceiling is already spent.

        Called before each request rather than after, so the ceiling is
        a limit on money spent rather than on money committed.

        A run that has spent nothing is always allowed to proceed, even
        under a ``--max-cost 0``. Otherwise that ceiling would also stop
        the free local control group, whose whole job is to run every
        week; read literally it means "free calls only", and a paid model
        under it stops after its first charged sample.
        """
        if self.spent_usd <= 0.0 or self.spent_usd < self.ceiling_usd:
            return
        if self.tripped_at_usd is None:
            self.tripped_at_usd = self.spent_usd
        raise BudgetExceeded(
            f"run has spent ${self.spent_usd:.2f} of its ${self.ceiling_usd:.2f} "
            f"--max-cost ceiling after {self.calls_charged} priced call(s); "
            f"refusing further requests. Samples already captured are kept."
        )

    def charge(self, sample: Sample) -> float:
        """Add one captured sample's cost to the tally, returning it.

        A sample that cannot be priced is charged nothing, because there
        is nothing to charge from, and is counted under the reason it
        could not be priced. Those counts are reported alongside the
        spend so an operator can tell "cheap run" from "the ceiling was
        never actually watching anything".
        """
        cost = sample_cost_usd(sample)
        if cost is None:
            if sample.input_tokens is None or sample.output_tokens is None:
                self.calls_missing_tokens += 1
            else:
                self.calls_no_price += 1
            return 0.0
        self.spent_usd += cost
        self.calls_charged += 1
        return cost

    def pretty(self) -> str:
        line = (
            f"budget: spent ${self.spent_usd:.2f} of the "
            f"${self.ceiling_usd:.2f} --max-cost ceiling "
            f"({self.calls_charged} priced call(s)"
        )
        if self.calls_missing_tokens:
            line += f", {self.calls_missing_tokens} with no token counts"
        if self.calls_no_price:
            line += f", {self.calls_no_price} with no price on file"
        return line + ")"


class BudgetGuardedRunner(Runner):
    """Wraps a runner so every request is checked against a shared ledger.

    Deliberately implements only :meth:`sample` and inherits
    :meth:`Runner.batch`, so the check lands on each individual request
    rather than once per batch.

    How far the ceiling can be overshot
    -----------------------------------
    Each runner's batch keeps its own in-flight window of
    ``sampling.concurrency_per_provider`` requests and only trips on its
    own next check, and the orchestrator gathers every runner
    concurrently against one shared ledger. The bound is therefore
    ``concurrency_per_provider x guarded runners sampling at the time``,
    not ``concurrency_per_provider``. It is 4 on the current config only
    because the alternating roster runs exactly one paid runner plus the
    free local baseline in any given week; a week with the full roster
    enabled would be six times that. Set the ceiling with that headroom
    in mind rather than at the exact number you cannot afford.

    Requests billed but not kept
    ----------------------------
    :meth:`sample` charges after the inner call returns, but
    :meth:`Runner.batch` creates all N tasks up front and cancels every
    remaining task when one raises. A :class:`BudgetExceeded` from
    ``ledger.check()`` completes instantly, with no network wait, so it
    reaches ``as_completed`` ahead of requests that are already on the
    wire. Up to ``concurrency_per_provider - 1`` of those get cancelled
    mid-flight: the provider bills them, the response is discarded rather
    than stored, and the ledger never charges them. So a budget stop can
    cost real money that appears in neither the ledger nor the run log's
    ``actual_cost_usd``, on exactly the runs where that number matters
    most. The cancel-on-error behaviour lives in
    ``meridian/runners/base.py`` and is not this class's to change;
    treat the ceiling as approximate to within that window.
    """

    def __init__(self, inner: Runner, ledger: BudgetLedger) -> None:
        self.inner = inner
        self.ledger = ledger
        # Mirror the identity the orchestrator, storage layer, and cost
        # estimator all key off. Copied rather than proxied via
        # __getattr__ so a missing attribute fails loudly here instead
        # of silently mislabelling stored samples.
        self.provider = inner.provider
        self.model_id = inner.model_id
        self.max_tokens_override = inner.max_tokens_override

    async def prepare(self) -> None:
        await self.inner.prepare()

    def supports_temperature(self, temperature: float) -> bool:
        return self.inner.supports_temperature(temperature)

    async def sample(
        self,
        prompt: str,
        *,
        prompt_id: str,
        request_index: int,
        temperature: float,
        max_tokens: int = 1024,
    ) -> Sample:
        self.ledger.check()
        s = await self.inner.sample(
            prompt,
            prompt_id=prompt_id,
            request_index=request_index,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.ledger.charge(s)
        return s


def guard_runners(
    runners: list[Runner], *, ceiling_usd: float
) -> tuple[list[Runner], BudgetLedger]:
    """Wrap every runner against one shared ledger.

    Shared on purpose: the ceiling is a limit on the run, not on each
    provider, so a week where one model is unexpectedly expensive stops
    the whole run rather than letting the others spend the rest.
    """
    ledger = BudgetLedger(ceiling_usd=ceiling_usd)
    return [BudgetGuardedRunner(r, ledger) for r in runners], ledger
