"""Ollama runner tests using respx to mock the HTTP layer."""
from __future__ import annotations

import httpx
import pytest
import respx

from meridian.runners.base import UpstreamError
from meridian.runners.ollama import OllamaRunner


@pytest.mark.asyncio
async def test_ollama_happy_path():
    async with httpx.AsyncClient() as client:
        runner = OllamaRunner("llama3.2:3b", client=client)
        with respx.mock(base_url="http://localhost:11434") as mock:
            mock.post("/api/generate").respond(
                200,
                json={
                    "model": "llama3.2:3b",
                    "response": "The capital of France is Paris.",
                    "done_reason": "stop",
                    "prompt_eval_count": 12,
                    "eval_count": 18,
                },
            )
            sample = await runner.sample(
                "Capital of France?",
                prompt_id="sci-geography",
                request_index=0,
                temperature=0.7,
            )
    assert sample.text.startswith("The capital")
    assert sample.model_version_string == "llama3.2:3b"
    assert sample.provider == "ollama"
    assert sample.stop_reason == "stop"
    assert sample.input_tokens == 12
    assert sample.output_tokens == 18


@pytest.mark.asyncio
async def test_ollama_server_error_is_upstream():
    async with httpx.AsyncClient() as client:
        runner = OllamaRunner(
            "llama3.2:3b",
            client=client,
        )
        with respx.mock(base_url="http://localhost:11434") as mock:
            mock.post("/api/generate").respond(500, text="kaboom")
            with pytest.raises(UpstreamError):
                # with_retry will retry a few times and then re-raise.
                await runner.sample(
                    "hi",
                    prompt_id="p",
                    request_index=0,
                    temperature=0.7,
                )


@pytest.mark.asyncio
async def test_ollama_batch_yields_n_samples():
    async with httpx.AsyncClient() as client:
        runner = OllamaRunner("llama3.2:3b", client=client)
        with respx.mock(base_url="http://localhost:11434") as mock:
            mock.post("/api/generate").respond(
                200,
                json={
                    "model": "llama3.2:3b",
                    "response": "ok",
                    "done_reason": "stop",
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                },
            )
            samples = []
            async for s in runner.batch(
                "hi",
                prompt_id="p",
                n=5,
                temperature=0.7,
                concurrency=2,
            ):
                samples.append(s)
    assert len(samples) == 5
    assert {s.request_index for s in samples} == {0, 1, 2, 3, 4}
