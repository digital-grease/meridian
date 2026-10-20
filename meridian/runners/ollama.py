"""Ollama runner — local baseline / control group.

Ollama serves weights locally; because they do not change unless the user
updates them, any drift in Ollama's outputs is pure sampling noise. That
noise floor is what we subtract from commercial-provider drift to isolate
real signal.

Protocol: POST /api/generate with ``{"model", "prompt", "options", "stream": false}``.
Returns JSON with ``response``, ``model``, ``done_reason``, ``prompt_eval_count``,
``eval_count``, and timing in nanoseconds.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import httpx

from meridian.runners._retry import with_retry
from meridian.runners.base import (
    IntegrityError,
    Runner,
    Sample,
    UpstreamError,
)

_log = logging.getLogger(__name__)


class OllamaRunner(Runner):
    provider = "ollama"

    def __init__(
        self,
        model_id: str,
        *,
        base_url: str = "http://localhost:11434",
        client: httpx.AsyncClient | None = None,
        # Per request. qwen3.6:27b on the A10G serves one request at a time
        # (measured 2026-10-20, OLLAMA_NUM_PARALLEL had no effect), so the
        # runner's 4 in-flight requests queue: the 4th waits for up to three
        # 1024-token answers at ~49 tok/s, about 84 s before its own begins.
        timeout: float = 300.0,
        # For the one-off model load in prepare(). Reading 17 GB of weights
        # from gp3 into the GPU took 140-228 s on a fresh boot.
        load_timeout: float = 900.0,
        expected_digest: str | None = None,
        max_tokens: int | None = None,
        think: bool | None = None,
    ) -> None:
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self.timeout = timeout
        self.load_timeout = load_timeout
        self.expected_digest = expected_digest
        self.max_tokens_override = max_tokens
        # None omits `think` from the request (the model's default). Engines
        # older than thinking support reject the field, so it is only sent
        # when a runner's config sets it.
        self.think = think

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def prepare(self) -> None:
        await self._verify_digest()
        await self._load_model()

    async def _load_model(self) -> None:
        """Load the weights before sampling starts.

        A generate request with no prompt only loads the model. Without
        this the first sampling requests of a cold boot wait out the load
        and hit the per-request timeout.

        A failed load is logged, not raised: an error from prepare() halts
        the runner for the week, and a load that merely failed to finish
        here may still succeed on the first sample, whose own retries
        surface a real failure.
        """
        client = self._get_client()
        try:
            resp = await client.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model_id},
                timeout=self.load_timeout,
            )
        except httpx.HTTPError as e:
            _log.warning("ollama: loading %s failed, sampling anyway: %s", self.model_id, e)
            return
        if resp.status_code != 200:
            _log.warning(
                "ollama: loading %s returned %s, sampling anyway: %s",
                self.model_id, resp.status_code, resp.text[:200],
            )

    async def _verify_digest(self) -> None:
        # No digest pinned → nothing to verify, behave as before.
        if self.expected_digest is None:
            return

        client = self._get_client()
        try:
            resp = await client.get(f"{self.base_url}/api/tags")
        except httpx.HTTPError as e:
            raise UpstreamError(f"ollama transport error during digest check: {e}") from e
        if resp.status_code != 200:
            raise UpstreamError(
                f"ollama /api/tags returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise UpstreamError(f"ollama /api/tags non-json body: {e}") from e

        for entry in body.get("models", []):
            if entry.get("name") == self.model_id or entry.get("model") == self.model_id:
                served = entry.get("digest")
                if served != self.expected_digest:
                    raise IntegrityError(
                        f"ollama model {self.model_id!r} digest mismatch: "
                        f"expected {self.expected_digest}, got {served}. "
                        "Refusing to sample — control-group invariance broken. "
                        "Either re-pin to the new digest after auditing the change, "
                        "or restore the previous model version."
                    )
                return
        raise IntegrityError(
            f"ollama model {self.model_id!r} not installed on server at {self.base_url}; "
            f"expected digest {self.expected_digest}. Run `ollama pull {self.model_id}`."
        )

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
            client = self._get_client()
            payload: dict = {
                "model": self.model_id,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            }
            if self.think is not None:
                payload["think"] = self.think
            try:
                resp = await client.post(
                    f"{self.base_url}/api/generate", json=payload,
                )
            except httpx.HTTPError as e:
                raise UpstreamError(f"ollama transport error: {e}") from e

            if resp.status_code >= 500:
                raise UpstreamError(f"ollama {resp.status_code}: {resp.text[:200]}")
            if resp.status_code >= 400:
                raise UpstreamError(f"ollama {resp.status_code}: {resp.text[:200]}")

            try:
                body = resp.json()
            except ValueError as e:
                raise UpstreamError(f"ollama non-json body: {e}") from e

            latency_ms = int((time.monotonic() - started) * 1000)
            return Sample(
                prompt_id=prompt_id,
                model_id=self.model_id,
                provider=self.provider,
                request_index=request_index,
                temperature=temperature,
                max_tokens=max_tokens,
                text=body.get("response", ""),
                model_version_string=body.get("model", self.model_id),
                stop_reason=body.get("done_reason"),
                finish_reason=None,
                input_tokens=body.get("prompt_eval_count"),
                output_tokens=body.get("eval_count"),
                request_id=None,
                api_version="ollama-http-v1",
                latency_ms=latency_ms,
                captured_at=datetime.now(timezone.utc),
                safety_flags=[],
            )

        return await with_retry(one_call)
