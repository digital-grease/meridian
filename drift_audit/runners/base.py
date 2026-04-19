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


class RateLimitError(RunnerError):
    """Upstream rate-limited us. Retry after ``retry_after_s`` if set."""

    def __init__(self, message: str, retry_after_s: float | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class UpstreamError(RunnerError):
    """Any other upstream failure. Includes transient 5xx and malformed responses."""


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
    ) -> AsyncIterator[Sample]:
        """Yield ``n`` samples with bounded concurrency.

        Callers receive samples in completion order, not request order.
        ``request_index`` is set sequentially on each Sample.
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
                yield await coro
        except BaseException:
            for t in tasks:
                t.cancel()
            raise
