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

from meridian.runners.anthropic import AnthropicRunner
from meridian.runners.openai import OpenAIRunner, _token_kwarg_for


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
