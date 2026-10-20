"""Ollama runner tests using respx to mock the HTTP layer."""
from __future__ import annotations

import httpx
import pytest
import respx

from meridian.runners.base import IntegrityError, UpstreamError
from meridian.runners.ollama import OllamaRunner

PINNED_DIGEST = "a" * 64
WRONG_DIGEST = "b" * 64


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


@pytest.mark.asyncio
async def test_ollama_prepare_no_pin_skips_digest_check_but_loads():
    async with httpx.AsyncClient() as client:
        runner = OllamaRunner("llama3.2:3b", client=client)
        # Without expected_digest, prepare() must not call /api/tags at all,
        # but still loads the model.
        with respx.mock(base_url="http://localhost:11434") as mock:
            load = mock.post("/api/generate").respond(200, json={"done": True})
            await runner.prepare()
            assert [c.request.url.path for c in mock.calls] == ["/api/generate"]
    import json
    assert json.loads(load.calls.last.request.content) == {"model": "llama3.2:3b"}


@pytest.mark.asyncio
async def test_ollama_prepare_load_failure_does_not_raise():
    # A load that fails must not halt the runner for the week; sampling's
    # own retries decide whether the model is really unavailable.
    async with httpx.AsyncClient() as client:
        runner = OllamaRunner("qwen3.6:27b", client=client)
        with respx.mock(base_url="http://localhost:11434") as mock:
            mock.post("/api/generate").mock(side_effect=httpx.ReadTimeout("slow load"))
            await runner.prepare()


@pytest.mark.asyncio
async def test_ollama_prepare_mismatch_does_not_load():
    async with httpx.AsyncClient() as client:
        runner = OllamaRunner("llama3.2:3b", client=client, expected_digest=PINNED_DIGEST)
        with respx.mock(base_url="http://localhost:11434", assert_all_called=False) as mock:
            mock.get("/api/tags").respond(
                200, json={"models": [{"name": "llama3.2:3b", "digest": WRONG_DIGEST}]},
            )
            load = mock.post("/api/generate").respond(200, json={"done": True})
            with pytest.raises(IntegrityError):
                await runner.prepare()
            assert not load.called


@pytest.mark.asyncio
async def test_ollama_prepare_digest_match_passes():
    async with httpx.AsyncClient() as client:
        runner = OllamaRunner(
            "llama3.2:3b", client=client, expected_digest=PINNED_DIGEST,
        )
        with respx.mock(base_url="http://localhost:11434") as mock:
            mock.get("/api/tags").respond(
                200,
                json={"models": [
                    {"name": "llama3.2:3b", "digest": PINNED_DIGEST},
                    {"name": "qwen2.5:7b", "digest": "c" * 64},
                ]},
            )
            mock.post("/api/generate").respond(200, json={"done": True})
            await runner.prepare()  # must not raise


@pytest.mark.asyncio
async def test_ollama_prepare_digest_mismatch_raises_integrity():
    async with httpx.AsyncClient() as client:
        runner = OllamaRunner(
            "llama3.2:3b", client=client, expected_digest=PINNED_DIGEST,
        )
        with respx.mock(base_url="http://localhost:11434") as mock:
            mock.get("/api/tags").respond(
                200,
                json={"models": [{"name": "llama3.2:3b", "digest": WRONG_DIGEST}]},
            )
            with pytest.raises(IntegrityError, match="digest mismatch"):
                await runner.prepare()


@pytest.mark.asyncio
async def test_ollama_prepare_model_not_installed_raises_integrity():
    async with httpx.AsyncClient() as client:
        runner = OllamaRunner(
            "llama3.2:3b", client=client, expected_digest=PINNED_DIGEST,
        )
        with respx.mock(base_url="http://localhost:11434") as mock:
            mock.get("/api/tags").respond(
                200,
                json={"models": [{"name": "qwen2.5:7b", "digest": "c" * 64}]},
            )
            with pytest.raises(IntegrityError, match="not installed"):
                await runner.prepare()


@pytest.mark.asyncio
async def test_ollama_prepare_transport_error_raises_upstream():
    async with httpx.AsyncClient() as client:
        runner = OllamaRunner(
            "llama3.2:3b", client=client, expected_digest=PINNED_DIGEST,
        )
        with respx.mock(base_url="http://localhost:11434") as mock:
            mock.get("/api/tags").mock(side_effect=httpx.ConnectError("nope"))
            with pytest.raises(UpstreamError):
                await runner.prepare()


async def _sent_payload(runner: OllamaRunner, base_url: str) -> dict:
    """Sample once against a mocked engine and return the JSON it was sent."""
    import json

    with respx.mock(base_url=base_url) as mock:
        route = mock.post("/api/generate").respond(
            200, json={"model": runner.model_id, "response": "ok", "done_reason": "stop"},
        )
        await runner.sample("hi", prompt_id="p", request_index=0, temperature=1.0)
    return json.loads(route.calls.last.request.content)


@pytest.mark.asyncio
async def test_ollama_omits_think_by_default():
    # Engines older than thinking support reject the field, so the
    # llama3.2:3b baseline on the 0.23.1 engine must never be sent it.
    async with httpx.AsyncClient() as client:
        runner = OllamaRunner("llama3.2:3b", client=client)
        payload = await _sent_payload(runner, "http://localhost:11434")
    assert "think" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("think", [False, True])
async def test_ollama_sends_think_when_set(think: bool):
    async with httpx.AsyncClient() as client:
        runner = OllamaRunner(
            "qwen3.6:27b", client=client,
            base_url="http://127.0.0.1:11435", think=think,
        )
        payload = await _sent_payload(runner, "http://127.0.0.1:11435")
    assert payload["think"] is think
    assert payload["options"]["num_predict"] == 1024


def test_shipped_config_wires_both_local_engines(monkeypatch):
    # llama3.2:3b stays on the system engine with no `think` field;
    # qwen3.6:27b goes to the pinned engine with thinking off. Both run
    # in 2026-W43, qwen's first week.
    from meridian.config import build_runners, load_config

    monkeypatch.setenv("MERIDIAN_SKIP_PROVIDERS", "anthropic,openai")
    runners = {
        r.model_id: r
        for r in build_runners(load_config(), week_id="2026-W43")
        if isinstance(r, OllamaRunner)
    }
    assert runners["llama3.2:3b"].base_url == "http://localhost:11434"
    assert runners["llama3.2:3b"].think is None
    assert runners["qwen3.6:27b"].base_url == "http://127.0.0.1:11435"
    assert runners["qwen3.6:27b"].think is False
    assert runners["qwen3.6:27b"].expected_digest is not None


def test_qwen_not_in_rosters_before_its_first_week(monkeypatch):
    from meridian.config import build_runners, load_config

    monkeypatch.setenv("MERIDIAN_SKIP_PROVIDERS", "anthropic,openai")
    ids = {r.model_id for r in build_runners(load_config(), week_id="2026-W42")}
    assert "qwen3.6:27b" not in ids
    assert "llama3.2:3b" in ids
