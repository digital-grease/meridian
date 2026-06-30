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

from meridian.runners._retry import with_retry
from meridian.runners.base import (
    AuthError,
    RateLimitError,
    Runner,
    Sample,
    UpstreamError,
)


#: Model-id prefixes whose API deprecates `temperature` (returns 400
#: "`temperature` is deprecated for this model" on non-default values).
#: Anthropic tolerates the API default (1.0) but nothing else. Extend
#: this list when a new thinking-default model errors with that message.
_TEMPERATURE_DEPRECATED_PREFIXES: tuple[str, ...] = (
    "claude-opus-4-7",
    "claude-opus-4-8",
)

#: Anthropic Messages API treats 1.0 as the default; only this value is
#: accepted by the prefixes above.
_ANTHROPIC_DEFAULT_TEMPERATURE = 1.0


def _anthropic_supports_temperature(model_id: str, temperature: float) -> bool:
    """Whether Anthropic's API will accept this temperature for this
    model. Exposed as a pure-Python helper so tests can exercise the
    decision without constructing a runner (which spins up an SDK
    client that expects credentials)."""
    mid = model_id.lower()
    if any(mid.startswith(p) for p in _TEMPERATURE_DEPRECATED_PREFIXES):
        return temperature == _ANTHROPIC_DEFAULT_TEMPERATURE
    return True


def _build_message_kwargs(
    *,
    model_id: str,
    prompt: str,
    temperature: float,
    max_tokens: int,
) -> dict:
    """Construct the kwargs dict for ``AsyncAnthropic.messages.create``.

    On thinking-by-default models (Opus 4.7 and successors), Anthropic
    has deprecated ``temperature``, ``top_p``, and ``top_k``. The API
    currently still tolerates the implicit default (1.0) but the
    documented migration is to omit the parameter entirely
    (https://platform.claude.com/docs/en/api/messages). Sending an
    explicit value is brittle if Anthropic ever changes the implicit
    default — so we omit the param up-front for these models. The
    Sample record still carries ``temperature`` as the orchestrator
    intended (1.0 in this case), even though the API call did not
    include it; that field documents our sampling intent, not the
    bytes on the wire.
    """
    kwargs: dict = {
        "model": model_id,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if not any(model_id.lower().startswith(p) for p in _TEMPERATURE_DEPRECATED_PREFIXES):
        kwargs["temperature"] = temperature
    return kwargs


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

    def supports_temperature(self, temperature: float) -> bool:
        return _anthropic_supports_temperature(self.model_id, temperature)

    async def sample(
        self,
        prompt: str,
        *,
        prompt_id: str,
        request_index: int,
        temperature: float,
        max_tokens: int = 1024,
    ) -> Sample:
        api_kwargs = _build_message_kwargs(
            model_id=self.model_id,
            prompt=prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        async def one_call() -> Sample:
            started = time.monotonic()
            try:
                resp = await self.client.messages.create(**api_kwargs)
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
