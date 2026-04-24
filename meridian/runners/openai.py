"""OpenAI / GPT runner.

Uses the official ``openai`` SDK (chat completions endpoint). The SDK
reports the exact deployed model string in ``response.model`` — critical
for detecting silent upstream upgrades.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import openai
from openai import APIStatusError, AsyncOpenAI

from meridian.runners._retry import with_retry
from meridian.runners.base import (
    AuthError,
    RateLimitError,
    Runner,
    Sample,
    UpstreamError,
)


class OpenAIRunner(Runner):
    provider = "openai"

    def __init__(
        self,
        model_id: str,
        *,
        api_key: str | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.model_id = model_id
        self.client = client or AsyncOpenAI(api_key=api_key)

    async def sample(
        self,
        prompt: str,
        *,
        prompt_id: str,
        request_index: int,
        temperature: float,
        max_tokens: int = 1024,
    ) -> Sample:
        token_kwarg = _token_kwarg_for(self.model_id)

        async def one_call() -> Sample:
            started = time.monotonic()
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=temperature,
                    **{token_kwarg: max_tokens},
                )
            except openai.AuthenticationError as e:
                raise AuthError(str(e)) from e
            except openai.RateLimitError as e:
                retry_after = _parse_retry_after(e)
                raise RateLimitError(str(e), retry_after_s=retry_after) from e
            except (openai.APITimeoutError, openai.APIConnectionError) as e:
                raise UpstreamError(str(e)) from e
            except APIStatusError as e:
                raise UpstreamError(str(e)) from e

            latency_ms = int((time.monotonic() - started) * 1000)
            choice = resp.choices[0] if resp.choices else None
            text = (choice.message.content or "") if choice else ""
            usage = getattr(resp, "usage", None)
            return Sample(
                prompt_id=prompt_id,
                model_id=self.model_id,
                provider=self.provider,
                request_index=request_index,
                temperature=temperature,
                max_tokens=max_tokens,
                text=text,
                model_version_string=resp.model,
                stop_reason=None,
                finish_reason=(choice.finish_reason if choice else None),
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
                request_id=getattr(resp, "id", None),
                api_version=f"openai-sdk-{openai.__version__}",
                latency_ms=latency_ms,
                captured_at=datetime.now(timezone.utc),
                safety_flags=[],
            )

        return await with_retry(one_call)


def _token_kwarg_for(model_id: str) -> str:
    """Return the token-cap parameter name the model's API accepts.

    GPT-5 family and o-series reasoning models (o1, o3, o4, ...)
    reject `max_tokens` with `unsupported_parameter`; they require
    `max_completion_tokens`. Legacy `gpt-4*` / `gpt-3.5*` still accept
    the older name. OpenAI flipped the default as part of the GPT-5
    rollout; the error message on a failed call is the signal to
    extend this list when new model families ship.
    """
    mid = model_id.lower()
    if mid.startswith(("gpt-5", "o1", "o3", "o4")):
        return "max_completion_tokens"
    return "max_tokens"


def _parse_retry_after(e: openai.RateLimitError) -> float | None:
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
