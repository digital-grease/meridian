"""Constructor-level tests for the SDK-backed runners.

Full HTTP-level coverage of the Anthropic and OpenAI SDKs would require
either pinning internal SDK implementation details or recording cassettes;
both are brittle. We instead verify that the runners instantiate with
sensible defaults and expose the expected ``provider`` / ``model_id``
attributes. Behavioral coverage of the shared retry + error-translation
paths lives in ``test_runner_ollama.py``.
"""
from __future__ import annotations

import pytest

from meridian.runners.anthropic import (
    AnthropicRunner,
    _anthropic_supports_temperature,
)
from meridian.runners.openai import (
    OpenAIRunner,
    _openai_supports_temperature,
    _token_kwarg_for,
)


def test_anthropic_runner_constructs():
    r = AnthropicRunner("claude-opus-4-7", api_key="sk-dummy")
    assert r.provider == "anthropic"
    assert r.model_id == "claude-opus-4-7"
    assert r.client is not None


def test_openai_runner_constructs():
    r = OpenAIRunner("gpt-5.1", api_key="sk-dummy")
    assert r.provider == "openai"
    assert r.model_id == "gpt-5.1"
    assert r.client is not None


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("gpt-5.1", "max_completion_tokens"),
        ("gpt-5-nano", "max_completion_tokens"),
        ("gpt-5", "max_completion_tokens"),
        ("o1-preview", "max_completion_tokens"),
        ("o3-mini", "max_completion_tokens"),
        ("o4-mini", "max_completion_tokens"),
        ("gpt-4.1-mini", "max_tokens"),
        ("gpt-4o", "max_tokens"),
        ("gpt-4", "max_tokens"),
        ("gpt-3.5-turbo", "max_tokens"),
    ],
)
def test_openai_token_kwarg_for_model(model_id: str, expected: str):
    """GPT-5 family and o-series reasoning models reject `max_tokens`
    and require `max_completion_tokens`. Legacy models keep the old
    name. The runner dispatches on a prefix test; this pins that
    contract so a typo in the prefix list can't silently regress."""
    assert _token_kwarg_for(model_id) == expected


@pytest.mark.parametrize(
    "model_id,temp,supported",
    [
        # Opus 4.7 accepts the API default (1.0) and rejects anything else
        # with "`temperature` is deprecated for this model".
        ("claude-opus-4-7", 1.0, True),
        ("claude-opus-4-7", 0.0, False),
        ("claude-opus-4-7", 0.5, False),
        ("claude-opus-4-7-20250114", 0.0, False),  # date-pinned alias
        # Non-thinking Claude families still accept any temperature.
        ("claude-sonnet-4-6", 0.0, True),
        ("claude-haiku-4-5-20251001", 0.5, True),
        ("claude-3-5-sonnet", 0.2, True),
    ],
)
def test_anthropic_supports_temperature(model_id: str, temp: float, supported: bool):
    """Pins the prefix list that controls which Anthropic models the
    orchestrator treats as temperature-deprecated."""
    assert _anthropic_supports_temperature(model_id, temp) is supported


@pytest.mark.parametrize(
    "model_id,temp,supported",
    [
        # o-series reasoning models reject `temperature` outright.
        ("o1-preview", 1.0, False),
        ("o1-mini", 0.0, False),
        ("o3", 0.5, False),
        ("o3-mini", 1.0, False),
        ("o4-mini", 0.0, False),
        # GPT-5 family currently accepts temperature.
        ("gpt-5.1", 0.0, True),
        ("gpt-5.1", 1.0, True),
        # Legacy GPT-4 family accepts temperature.
        ("gpt-4o", 0.0, True),
        ("gpt-4.1-mini", 1.0, True),
    ],
)
def test_openai_supports_temperature(model_id: str, temp: float, supported: bool):
    assert _openai_supports_temperature(model_id, temp) is supported
