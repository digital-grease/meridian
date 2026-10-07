"""An account that cannot pay is a verdict on the runner, not a flaky request.

2026-W36 is the case. Anthropic's prepaid balance ran out, every request
came back HTTP 400 "Your credit balance is too low to access the
Anthropic API", the runner classified that as :class:`UpstreamError`,
``with_retry`` tried each one four times, and the run log held 59
generic upstream errors that read like an outage.

The tests here pin the four parts of the fix: the provider responses
that mean "the account cannot pay" (or "the key is not accepted") map
to their own classes, those classes are never retried, the orchestrator
stops sending requests for a runner after the first one, and the
preflight probe turns the same failure into one record before any
prompt is attempted. They also pin the boundary the other way, because
a false positive halts a runner for the rest of the week.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest

from meridian.config import PipelineConfig
from meridian.corpus import load_corpus
from meridian.pipeline.run_log import append_run_log, read_run_log
from meridian.runners._retry import with_retry
from meridian.runners.anthropic import AnthropicRunner
from meridian.runners.anthropic import _map_error as anthropic_map_error
from meridian.runners.base import (
    AuthError,
    BillingError,
    ContentPolicyError,
    RateLimitError,
    Runner,
    Sample,
    UpstreamError,
)
from meridian.runners.openai import OpenAIRunner
from meridian.runners.openai import _map_error as openai_map_error
from meridian.sampling.orchestrator import Orchestrator, SamplingPlan
from meridian.storage import LocalSampleStore

FIXTURES = Path(__file__).parent / "fixtures"

# The real 2026-W36 run-log row, copied from the published log. Its
# errors are the messages the detector has to recognise, verbatim (the
# log truncates each to 200 characters, which is still enough).
W36_ROW = json.loads((FIXTURES / "run_log_2026-W36.jsonl").read_text())

W36_MESSAGE = W36_ROW["errors"][0]["message"]

W36_BODY = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": (
            "Your credit balance is too low to access the Anthropic API. "
            "Please go to Plans & Billing to upgrade or purchase credits."
        ),
    },
}


def _anthropic_error(
    message: str, *, status: int = 400, body: object | None = None
) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    cls = {
        400: anthropic.BadRequestError,
        401: anthropic.AuthenticationError,
        403: anthropic.PermissionDeniedError,
        429: anthropic.RateLimitError,
        500: anthropic.InternalServerError,
    }.get(status, anthropic.APIStatusError)
    return cls(message, response=response, body=body)


def _openai_error(
    message: str, *, status: int, body: object | None = None
) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status, request=request)
    cls = {
        400: openai.BadRequestError,
        401: openai.AuthenticationError,
        403: openai.PermissionDeniedError,
        429: openai.RateLimitError,
        500: openai.InternalServerError,
    }.get(status, openai.APIStatusError)
    return cls(message, response=response, body=body)


# OpenAI's SDK hands the inner `error` object to the exception as `body`.
QUOTA_BODY = {
    "message": "You exceeded your current quota, please check your plan "
    "and billing details.",
    "type": "insufficient_quota",
    "param": None,
    "code": "insufficient_quota",
}
HARD_LIMIT_BODY = {
    "message": "Billing hard limit has been reached",
    "type": "invalid_request_error",
    "param": None,
    "code": "billing_hard_limit_reached",
}


# ---------- Anthropic mapping ---------------------------------------


def test_anthropic_w36_credit_400_is_billing():
    err = anthropic_map_error(_anthropic_error(W36_MESSAGE, body=W36_BODY))

    assert isinstance(err, BillingError)


def test_every_w36_error_in_the_published_row_is_billing():
    """Replays the real row. Each of those 50 records was logged as an
    UpstreamError; under the new mapping each is a BillingError."""
    assert W36_ROW["errors"]
    for record in W36_ROW["errors"]:
        assert record["provider"] == "anthropic"
        err = anthropic_map_error(_anthropic_error(record["message"]))
        assert isinstance(err, BillingError), record["message"]


def test_anthropic_402_and_billing_error_type_are_billing():
    assert isinstance(
        anthropic_map_error(_anthropic_error("Error code: 402", status=402)),
        BillingError,
    )
    body = {"type": "error", "error": {"type": "billing_error", "message": "x"}}
    assert isinstance(
        anthropic_map_error(_anthropic_error("Error code: 400", body=body)),
        BillingError,
    )


@pytest.mark.parametrize("status", [401, 403])
def test_anthropic_auth_statuses_are_auth(status: int):
    err = anthropic_map_error(_anthropic_error("denied", status=status))

    assert isinstance(err, AuthError)
    assert not isinstance(err, BillingError)


def test_anthropic_ordinary_400_stays_upstream():
    """The 2026-W27 class of fault: deterministic, but a bug in our
    request. Calling it billing would halt the runner for the week."""
    msg = (
        "Error code: 400 - {'type': 'error', 'error': {'type': "
        "'invalid_request_error', 'message': '`temperature` is deprecated "
        "for this model.'}}"
    )
    assert type(anthropic_map_error(_anthropic_error(msg))) is UpstreamError


def test_anthropic_credit_marker_only_counts_on_400():
    err = anthropic_map_error(_anthropic_error(W36_MESSAGE, status=500))

    assert type(err) is UpstreamError


def test_anthropic_rate_limit_stays_rate_limit():
    err = anthropic_map_error(_anthropic_error("slow down", status=429))

    assert isinstance(err, RateLimitError)


# ---------- OpenAI mapping ------------------------------------------


def test_openai_insufficient_quota_429_is_billing_not_rate_limit():
    """The trap: OpenAI reports an exhausted quota as a 429, which would
    otherwise be retried with backoff and logged as rate limiting."""
    err = openai_map_error(
        _openai_error("Error code: 429", status=429, body=QUOTA_BODY)
    )

    assert isinstance(err, BillingError)
    assert not isinstance(err, RateLimitError)


@pytest.mark.parametrize("status", [400, 429])
def test_openai_billing_hard_limit_is_billing(status: int):
    err = openai_map_error(
        _openai_error("Error code", status=status, body=HARD_LIMIT_BODY)
    )

    assert isinstance(err, BillingError)


@pytest.mark.parametrize("status", [401, 403])
def test_openai_auth_statuses_are_auth(status: int):
    err = openai_map_error(_openai_error("denied", status=status))

    assert isinstance(err, AuthError)


def test_openai_plain_429_stays_rate_limit():
    body = {"message": "Rate limit reached", "type": "requests",
            "code": "rate_limit_exceeded"}
    err = openai_map_error(_openai_error("Error code: 429", status=429, body=body))

    assert isinstance(err, RateLimitError)


def test_openai_content_policy_still_maps_to_content_policy():
    msg = "This content was flagged for possible cybersecurity risk."
    err = openai_map_error(_openai_error(msg, status=400))

    assert isinstance(err, ContentPolicyError)


def test_openai_billing_prose_without_code_is_not_billing():
    """Prose is not trusted on this side: only the code counts."""
    err = openai_map_error(
        _openai_error("please check your plan and billing details", status=400)
    )

    assert type(err) is UpstreamError


# ---------- no retry -------------------------------------------------


class _AnthropicClient:
    """Stand-in for AsyncAnthropic. Raises ``exc`` if set, counts calls,
    and keeps the kwargs of each call."""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return SimpleNamespace(
            model="claude-opus-5-20260901", content=[], stop_reason="max_tokens",
            usage=SimpleNamespace(input_tokens=8, output_tokens=1), id="msg_1",
        )


class _OpenAIClient:
    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return SimpleNamespace(model="gpt-5.5-2026-06-30", choices=[], usage=None,
                               id="chatcmpl-1")


async def test_with_retry_does_not_retry_billing_or_auth():
    for cls in (BillingError, AuthError):
        calls = 0

        async def fails():
            nonlocal calls
            calls += 1
            raise cls("no")

        with pytest.raises(cls):
            await with_retry(fails, max_attempts=4, min_wait=0.0, max_wait=0.0)
        assert calls == 1, cls.__name__


async def test_anthropic_w36_credit_error_is_sent_once():
    client = _AnthropicClient(_anthropic_error(W36_MESSAGE, body=W36_BODY))
    runner = AnthropicRunner("claude-opus-5", client=client)

    with pytest.raises(BillingError):
        await runner.sample("q", prompt_id="pol-abortion-legal",
                            request_index=0, temperature=1.0)

    assert len(client.calls) == 1


async def test_openai_insufficient_quota_is_sent_once():
    client = _OpenAIClient(_openai_error("Error code: 429", status=429,
                                         body=QUOTA_BODY))
    runner = OpenAIRunner("gpt-5.5", client=client)

    with pytest.raises(BillingError):
        await runner.sample("q", prompt_id="pol-abortion-legal",
                            request_index=0, temperature=1.0)

    assert len(client.calls) == 1


# ---------- prepare() probe -----------------------------------------


async def test_anthropic_prepare_sends_one_minimal_request():
    client = _AnthropicClient()
    runner = AnthropicRunner("claude-opus-5", client=client)

    await runner.prepare()

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["max_tokens"] == 1
    # Omitted so it is valid on models that reject a non-default value.
    assert "temperature" not in call


async def test_anthropic_prepare_raises_billing_on_empty_balance():
    client = _AnthropicClient(_anthropic_error(W36_MESSAGE, body=W36_BODY))
    runner = AnthropicRunner("claude-opus-5", client=client)

    with pytest.raises(BillingError):
        await runner.prepare()
    assert len(client.calls) == 1


async def test_anthropic_prepare_raises_auth_on_rejected_key():
    client = _AnthropicClient(_anthropic_error("invalid x-api-key", status=401))
    runner = AnthropicRunner("claude-opus-5", client=client)

    with pytest.raises(AuthError):
        await runner.prepare()


@pytest.mark.parametrize("status", [400, 429, 500])
async def test_anthropic_prepare_swallows_inconclusive_failures(status: int):
    """An outage or a parameter quirk on the probe must not cost the
    week: the real requests may still succeed."""
    client = _AnthropicClient(_anthropic_error("nope", status=status))
    runner = AnthropicRunner("claude-opus-5", client=client)

    await runner.prepare()  # must not raise


async def test_openai_prepare_respects_gpt55_quirks():
    client = _OpenAIClient()
    runner = OpenAIRunner("gpt-5.5", client=client)

    await runner.prepare()

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["max_completion_tokens"] == 1
    assert "max_tokens" not in call
    # gpt-5.5 accepts only the default; omitting sends the default.
    assert "temperature" not in call


async def test_openai_prepare_uses_max_tokens_for_legacy_models():
    client = _OpenAIClient()
    await OpenAIRunner("gpt-4o", client=client).prepare()

    assert client.calls[0]["max_tokens"] == 1


async def test_openai_prepare_raises_billing_on_insufficient_quota():
    client = _OpenAIClient(_openai_error("Error code: 429", status=429,
                                         body=QUOTA_BODY))

    with pytest.raises(BillingError):
        await OpenAIRunner("gpt-5.5", client=client).prepare()


async def test_openai_prepare_raises_auth_on_403():
    client = _OpenAIClient(_openai_error("forbidden", status=403))

    with pytest.raises(AuthError):
        await OpenAIRunner("gpt-5.5", client=client).prepare()


@pytest.mark.parametrize("status", [400, 429, 500])
async def test_openai_prepare_swallows_inconclusive_failures(status: int):
    client = _OpenAIClient(_openai_error("nope", status=status))

    await OpenAIRunner("gpt-5.5", client=client).prepare()  # must not raise


# ---------- orchestrator circuit breaker ----------------------------


class _ScriptedRunner(Runner):
    """Raises ``exc`` once its ``fail_after`` successful samples are
    used up, and records the prompt of every request it receives."""

    def __init__(self, provider: str, model_id: str, *,
                 exc: Exception | None = None, fail_after: int = 0,
                 prepare_exc: Exception | None = None) -> None:
        self.provider = provider
        self.model_id = model_id
        self._exc = exc
        self._remaining = fail_after
        self._prepare_exc = prepare_exc
        self.calls = 0
        self.prompts_requested: set[str] = set()

    async def prepare(self) -> None:
        if self._prepare_exc is not None:
            raise self._prepare_exc

    async def sample(self, prompt, *, prompt_id, request_index, temperature,
                     max_tokens=1024) -> Sample:
        self.calls += 1
        self.prompts_requested.add(prompt_id)
        if self._exc is not None and self._remaining <= 0:
            raise self._exc
        self._remaining -= 1
        return Sample(
            prompt_id=prompt_id, model_id=self.model_id, provider=self.provider,
            request_index=request_index, temperature=temperature,
            max_tokens=max_tokens, text="an answer",
            model_version_string=self.model_id, stop_reason="end_turn",
            latency_ms=1, captured_at=datetime.now(timezone.utc),
        )


def _plan() -> SamplingPlan:
    return SamplingPlan(week_id="2026-W40", n_default_temp=2, n_zero_temp=0,
                        concurrency_per_provider=1)


def _prompts(n: int):
    return load_corpus().all()[:n]


@pytest.mark.parametrize("exc_cls", [BillingError, AuthError])
async def test_breaker_stops_requests_after_first_account_failure(
    tmp_path: Path, exc_cls
):
    prompts = _prompts(5)
    # Two samples succeed (the whole first pair), then the account dies
    # on the first request of the second pair.
    dead = _ScriptedRunner("anthropic", "claude-opus-5",
                           exc=exc_cls("credit balance is too low"), fail_after=2)
    ok = _ScriptedRunner("ollama", "llama3.2:3b")
    store = LocalSampleStore(tmp_path)

    outcome = await Orchestrator([dead, ok], store, load_corpus(), _plan()).run(
        prompts=prompts
    )

    # Requests already in flight inside the failing pair may still land
    # (the batch cancels them as soon as it can), but no later pair is
    # ever requested.
    assert dead.prompts_requested == {prompts[0].id, prompts[1].id}
    assert ok.calls == 2 * len(prompts)
    assert outcome.pairs_complete == 1 + len(prompts)
    assert outcome.pairs_failed == len(prompts) - 1

    dead_errors = [e for e in outcome.errors if e.provider == "anthropic"]
    assert [e.prompt_id for e in dead_errors] == [p.id for p in prompts[1:]]
    assert {e.error_type for e in dead_errors} == {exc_cls.__name__}
    assert all(e.message.startswith("not attempted") for e in dead_errors[1:])

    halt = outcome.runner_halts["anthropic/claude-opus-5"]
    assert halt["error_type"] == exc_cls.__name__
    assert halt["stage"] == "sample"
    assert halt["prompt_id"] == prompts[1].id
    assert halt["pairs_not_attempted"] == len(prompts) - 2
    assert "ollama/llama3.2:3b" not in outcome.runner_halts


async def test_breaker_ignores_ordinary_upstream_errors(tmp_path: Path):
    """A transient fault fails its own pair and nothing else."""
    prompts = _prompts(3)
    flaky = _ScriptedRunner("openai", "gpt-5.5", exc=UpstreamError("502"))

    outcome = await Orchestrator(
        [flaky], LocalSampleStore(tmp_path), load_corpus(), _plan()
    ).run(prompts=prompts)

    assert outcome.pairs_failed == len(prompts)
    assert outcome.runner_halts == {}
    assert all(e.message == "502" for e in outcome.errors)


async def test_breaker_still_skips_pairs_that_are_already_complete(tmp_path: Path):
    """A resumed run must not report stored data as failed."""
    prompts = _prompts(3)
    store = LocalSampleStore(tmp_path)
    corpus = load_corpus()
    await Orchestrator(
        [_ScriptedRunner("anthropic", "claude-opus-5")], store, corpus, _plan()
    ).run(prompts=prompts[2:])

    dead = _ScriptedRunner("anthropic", "claude-opus-5",
                           exc=BillingError("credit balance is too low"))
    outcome = await Orchestrator([dead], store, corpus, _plan()).run(prompts=prompts)

    assert dead.prompts_requested == {prompts[0].id}
    assert outcome.pairs_skipped == 1
    assert outcome.pairs_failed == 2


@pytest.mark.parametrize("exc_cls", [BillingError, AuthError])
async def test_prepare_failure_is_one_typed_record_and_no_requests(
    tmp_path: Path, exc_cls
):
    prompts = _prompts(4)
    dead = _ScriptedRunner("openai", "gpt-5.5", prepare_exc=exc_cls("no quota"))

    outcome = await Orchestrator(
        [dead], LocalSampleStore(tmp_path), load_corpus(), _plan()
    ).run(prompts=prompts)

    assert dead.calls == 0
    assert outcome.pairs_failed == len(prompts)
    assert len(outcome.errors) == 1
    err = outcome.errors[0]
    assert (err.prompt_id, err.error_type) == ("*", exc_cls.__name__)
    halt = outcome.runner_halts["openai/gpt-5.5"]
    assert halt["stage"] == "prepare"
    assert halt["error_type"] == exc_cls.__name__
    assert halt["pairs_not_attempted"] == len(prompts)


# ---------- run log --------------------------------------------------


def _config() -> PipelineConfig:
    return PipelineConfig.model_validate({"runners": []})


async def test_runner_halts_reach_the_run_log(tmp_path: Path):
    prompts = _prompts(3)
    dead = _ScriptedRunner("anthropic", "claude-opus-5",
                           prepare_exc=BillingError("credit balance is too low"))
    outcome = await Orchestrator(
        [dead], LocalSampleStore(tmp_path / "store"), load_corpus(), _plan()
    ).run(prompts=prompts)

    log = tmp_path / "run_log.jsonl"
    now = datetime.now(timezone.utc)
    append_run_log(log, started_at=now, finished_at=now, week_id="2026-W40",
                   config=_config(), outcome=outcome,
                   estimated_cost_usd=0.0, actual_cost_usd=0.0)

    row = json.loads(log.read_text().splitlines()[-1])
    assert row["errors"][0]["error_type"] == "BillingError"
    assert row["runner_halts"]["anthropic/claude-opus-5"]["error_type"] == "BillingError"
    assert read_run_log(log)[-1].runner_halts == row["runner_halts"]


def test_published_rows_without_runner_halts_still_parse(tmp_path: Path):
    log = tmp_path / "run_log.jsonl"
    log.write_text((FIXTURES / "run_log_2026-W36.jsonl").read_text())

    (entry,) = read_run_log(log)

    assert entry.week_id == "2026-W36"
    assert entry.runner_halts == {}
