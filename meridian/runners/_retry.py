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
    stop_after_attempt,
    wait_exponential,
)

from meridian.runners.base import (
    AuthError,
    RateLimitError,
    RunnerError,
    UpstreamError,
)

T = TypeVar("T")
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
    """
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=min_wait, max=max_wait),
        retry=retry_if_exception_type((RateLimitError, UpstreamError)),
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
            except AuthError:
                # Tenacity would retry this if we let it; AuthError is terminal.
                raise
            except RunnerError:
                raise
    raise RetryError("unreachable")  # pragma: no cover
