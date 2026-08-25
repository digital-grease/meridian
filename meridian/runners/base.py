"""Runner protocol and common types.

A Runner queries exactly one LLM and yields Samples. All provider-specific
details — SDK quirks, auth, rate limits, response metadata shape — are
confined to the concrete Runner subclass. The orchestrator only knows about
the Runner interface.
"""
from __future__ import annotations

import abc
import asyncio
from datetime import datetime, timezone
from typing import AsyncIterator

from pydantic import BaseModel, ConfigDict, Field


class RunnerError(Exception):
    """Base class for runner failures. Subclasses classify the failure mode."""


class AuthError(RunnerError):
    """Missing or invalid credentials."""


class IntegrityError(RunnerError):
    """A runner-level invariant was violated.

    Raised when a runner detects that its environment doesn't match what the
    pipeline expects (e.g. a pinned model digest doesn't match what the
    server is actually serving). The pipeline should never proceed past
    this — the resulting samples would be silently mislabelled.
    """


class RateLimitError(RunnerError):
    """Upstream rate-limited us. Retry after ``retry_after_s`` if set."""

    def __init__(self, message: str, retry_after_s: float | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class UpstreamError(RunnerError):
    """Any other upstream failure. Includes transient 5xx and malformed responses."""


class ContentPolicyError(RunnerError):
    """The provider rejected the REQUEST on content grounds. No completion exists.

    Split out of :class:`UpstreamError` because it violates that class's
    one promise. UpstreamError means "transient", and
    :func:`meridian.runners._retry.with_retry` acts on that by retrying
    it four times with exponential backoff. A content-policy 400 is
    deterministic: the same prompt returns the same rejection every
    time, so those four attempts only spend wall clock and rate-limit
    budget before recording the failure they were always going to
    record. First seen on 2026-W33, where ``openai/gpt-5.5`` rejected
    ``ref-wifi-unauthorized`` with "This content was flagged for
    possible cybersecurity risk".

    Deliberately NOT a refusal, and the distinction is the one
    :mod:`meridian.analysis.usability` already draws in
    ``_API_REFUSAL_REASONS``: a refusal is the MODEL declining in a
    response we received, while this is the PLATFORM declining to run
    the prompt at all. The model never saw it. Folding this into the
    refusal rate would publish a model behaviour that was never
    observed, and on the refusal-boundary axis specifically, which is
    the axis least able to absorb an invented data point.

    What it is instead, and whether the fact deserves its own published
    outcome, is a live question. It is a real measurement of something
    (the provider will not accept this prompt) and discarding it loses
    that. But it is a measurement of the platform, not the model, and
    the corpus currently has nowhere to put one.
    """


class Sample(BaseModel):
    """One response captured from one model for one prompt.

    Fields track the minimum metadata needed for drift analysis and for an
    auditor to reproduce or contest any finding. Add fields, never remove —
    append-only storage means old samples must remain parseable forever.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_id: str
    model_id: str
    provider: str

    # Request shape
    request_index: int               # 0..N-1 within this sampling run
    temperature: float
    max_tokens: int

    # Response text + provider metadata
    text: str
    model_version_string: str         # exact version the provider reported
    stop_reason: str | None = None
    finish_reason: str | None = None  # OpenAI-style synonym; providers differ
    input_tokens: int | None = None
    output_tokens: int | None = None

    # Transport/observability
    request_id: str | None = None
    api_version: str | None = None    # SDK or API version at capture
    latency_ms: int = Field(ge=0)
    captured_at: datetime

    # Provider-reported safety signals (structure intentionally loose — each
    # provider does it differently; downstream analysis normalizes).
    safety_flags: list[str] = Field(default_factory=list)

    @classmethod
    def now(cls, **kwargs) -> "Sample":
        """Convenience: fill in captured_at with the current UTC instant."""
        return cls(captured_at=datetime.now(timezone.utc), **kwargs)


class Runner(abc.ABC):
    """Abstract base for a single-model runner.

    Subclasses MUST set ``model_id`` and ``provider`` and implement
    :meth:`sample`. They SHOULD NOT override :meth:`batch` — the default
    bounded-concurrency implementation is usually what you want.
    """

    model_id: str
    provider: str

    #: Per-runner completion cap, overriding ``SamplingPlan.max_tokens``
    #: when set. Exists because the shared cap is a poor fit for
    #: reasoning-default models: on those, the cap covers reasoning
    #: tokens *plus* visible output, so a budget that is generous for a
    #: non-reasoning model can be consumed entirely by reasoning and
    #: return an empty completion with ``finish_reason="length"``. See
    #: ``meridian/analysis/usability.py`` for what happens downstream
    #: when that goes unnoticed.
    max_tokens_override: int | None = None

    async def prepare(self) -> None:
        """One-shot setup hook called by the orchestrator before any sample call.

        Default no-op. Subclasses override to validate environment invariants
        that need a network round-trip (e.g. ``OllamaRunner`` verifies the
        served model digest matches the pinned one). Failure should raise an
        :class:`IntegrityError`; the orchestrator treats that as fatal for
        this runner — no samples are written under a mismatched digest.
        """
        return None

    @abc.abstractmethod
    async def sample(
        self,
        prompt: str,
        *,
        prompt_id: str,
        request_index: int,
        temperature: float,
        max_tokens: int = 1024,
    ) -> Sample:
        """Return one Sample. Raises RunnerError on failure."""
        raise NotImplementedError

    def supports_temperature(self, temperature: float) -> bool:
        """True if this runner's model accepts ``temperature`` at the API.

        The default is permissive (True). Override in subclasses where a
        specific model rejects non-default temperature at the API (e.g.
        Anthropic's thinking-by-default Opus family returns 400 with
        "`temperature` is deprecated for this model" on non-default
        values; OpenAI's o1/o3 reasoning models have the same restriction).

        The orchestrator calls this before launching a batch at a given
        temperature and skips that batch — rather than burning requests
        on 400 errors — when it returns False.
        """
        return True

    async def batch(
        self,
        prompt: str,
        *,
        prompt_id: str,
        n: int,
        temperature: float,
        max_tokens: int = 1024,
        concurrency: int = 4,
        start_index: int = 0,
        rejections_out: list[ContentPolicyError] | None = None,
    ) -> AsyncIterator[Sample]:
        """Yield ``n`` samples with bounded concurrency.

        Callers receive samples in completion order, not request order.
        ``request_index`` is set sequentially on each Sample.

        Any error still cancels the remaining requests and propagates,
        with one exception. When ``rejections_out`` is provided, a
        :class:`ContentPolicyError` on an individual request is appended
        to it and the batch CONTINUES.

        That exception exists because of what the all-or-nothing version
        cost. 2026-W33 sampled ``gpt-5.5`` on ``ref-wifi-unauthorized``:
        requests 0 and 1 returned prose refusals from the model,
        something in flight beside them came back as a platform content
        rejection, and the raise took the other eighteen requests with
        it. The published cell was ``n_samples=2``, flagged "insufficient
        data", for a model that was answering the prompt perfectly well.
        The filter is evidently probabilistic rather than absolute, since
        it passed two of the first few and blocked another, so throwing
        away the whole cell on one hit discards a measurement that was
        working.

        A rejection is not a sample and is never yielded. It is counted,
        published as ``rejected_samples`` on the cell, and left out of
        every metric, because "the platform would not run this request"
        is a fact about the platform and the corpus measures models.
        """
        sem = asyncio.Semaphore(concurrency)

        async def one(idx: int) -> Sample:
            async with sem:
                return await self.sample(
                    prompt,
                    prompt_id=prompt_id,
                    request_index=idx,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

        tasks = [asyncio.create_task(one(start_index + i)) for i in range(n)]
        try:
            for coro in asyncio.as_completed(tasks):
                try:
                    yield await coro
                except ContentPolicyError as e:
                    if rejections_out is None:
                        raise
                    rejections_out.append(e)
        except BaseException:
            for t in tasks:
                t.cancel()
            raise
