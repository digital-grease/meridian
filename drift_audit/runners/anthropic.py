"""Anthropic / Claude runner.

Uses the official ``anthropic`` SDK. Captures the full set of metadata the
auditor will need to reproduce or contest a finding: exact model version
string as Anthropic reports it, stop reason, input/output token counts,
request id, and wall-clock latency.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import anthropic
from anthropic import APIStatusError, AsyncAnthropic

from drift_audit.runners._retry import with_retry
from drift_audit.runners.base import (
    AuthError,
    RateLimitError,
    Runner,
    Sample,
    UpstreamError,
)


class AnthropicRunner(Runner):
    provider = "anthropic"

    def __init__(
        self,
        model_id: str,
        *,
        api_key: str | None = None,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self.model_id = model_id
        self.client = client or AsyncAnthropic(api_key=api_key)

    async def sample(
        self,
        prompt: str,
        *,
        prompt_id: str,
        request_index: int,
        temperature: float,
        max_tokens: int = 1024,
    ) -> Sample:
        async def one_call() -> Sample:
            started = time.monotonic()
            try:
                resp = await self.client.messages.create(
                    model=self.model_id,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    messages=[{"role": "user", "content": prompt}],
                )
            except anthropic.AuthenticationError as e:
                raise AuthError(str(e)) from e
            except anthropic.RateLimitError as e:
                retry_after = _parse_retry_after(e)
                raise RateLimitError(str(e), retry_after_s=retry_after) from e
            except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
                raise UpstreamError(str(e)) from e
            except APIStatusError as e:
                if e.status_code in (500, 502, 503, 504):
                    raise UpstreamError(str(e)) from e
                raise UpstreamError(str(e)) from e

            latency_ms = int((time.monotonic() - started) * 1000)
            text = _extract_text(resp)
            return Sample(
                prompt_id=prompt_id,
                model_id=self.model_id,
                provider=self.provider,
                request_index=request_index,
                temperature=temperature,
                max_tokens=max_tokens,
                text=text,
                model_version_string=resp.model,
                stop_reason=resp.stop_reason,
                finish_reason=None,
                input_tokens=getattr(resp.usage, "input_tokens", None),
                output_tokens=getattr(resp.usage, "output_tokens", None),
                request_id=getattr(resp, "id", None),
                api_version=f"anthropic-sdk-{anthropic.__version__}",
                latency_ms=latency_ms,
                captured_at=datetime.now(timezone.utc),
                safety_flags=[],
            )

        return await with_retry(one_call)


def _extract_text(resp) -> str:
    """Concatenate text from Anthropic's block list response."""
    parts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts)


def _parse_retry_after(e: anthropic.RateLimitError) -> float | None:
    resp = getattr(e, "response", None)
    if resp is None:
        return None
    hdr = resp.headers.get("retry-after") if hasattr(resp, "headers") else None
    if hdr is None:
        return None
    try:
        return float(hdr)
    except (TypeError, ValueError):
        return None
