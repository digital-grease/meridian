"""Retry wrapper shared across runners.

Classifies exceptions into retryable / non-retryable, honors
``RateLimitError.retry_after_s`` when the provider surfaces it, and gives up
after a bounded number of attempts so a failing provider does not stall the
whole week's run.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from meridian.runners.base import (
    AuthError,
    BillingError,
    RateLimitError,
    RunnerError,
    UpstreamError,
)

T = TypeVar("T")

#: Failures of the account rather than the request. Never retried.
_TERMINAL: tuple[type[RunnerError], ...] = (AuthError, BillingError)

_log = logging.getLogger(__name__)


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 4,
    min_wait: float = 0.5,
    max_wait: float = 30.0,
) -> T:
    """Call ``fn`` with retry on transient errors.

    Retries on :class:`RateLimitError` (respecting ``retry_after_s``) and
    :class:`UpstreamError`. Does not retry on :class:`AuthError` — missing
    credentials will not become present by trying again.

    Nor on :class:`ContentPolicyError`, for the same reason stated
    differently: the provider has declined the prompt itself, and a
    prompt does not become acceptable by being sent again. That one is
    load-bearing rather than merely tidy. Those rejections land on the
    ``ref-`` prompts, the corpus samples each prompt up to 25 times, and
    the commercial roster alternates weekly, so classifying one as
    transient turns a single deterministic refusal into a hundred
    pointless round trips against a rate-limited API, every other week,
    forever.

    Nor on :class:`BillingError`. An empty balance or an exhausted quota
    is a state of the account, and it does not refill between attempts.
    The provider also reports some of these under a retryable-looking
    status (OpenAI sends ``insufficient_quota`` as a 429), which is why
    the runners map them to their own class before this wrapper sees
    them rather than leaving them to look like a rate limit.

    Retryable classes are named explicitly rather than excluded by
    subtraction. A new RunnerError subclass is therefore not retried
    until someone decides it should be, which is the safe default: the
    cost of not retrying something transient is one failed pair in the
    run log, and the cost of retrying something deterministic is the
    paragraph above.
    """
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=min_wait, max=max_wait),
        retry=(
            retry_if_exception_type((RateLimitError, UpstreamError))
            & retry_if_not_exception_type(_TERMINAL)
        ),
        reraise=True,
    ):
        with attempt:
            try:
                return await fn()
            except RateLimitError as e:
                if e.retry_after_s is not None:
                    _log.warning("rate-limited; sleeping %.1fs", e.retry_after_s)
                    await asyncio.sleep(e.retry_after_s)
                raise
            except _TERMINAL:
                # Terminal for the account, not just this request. The
                # predicate above also excludes them by name, so they stay
                # unretried even if one is ever reparented under a
                # retryable class.
                raise
            except RunnerError:
                raise
    raise RetryError("unreachable")  # pragma: no cover
