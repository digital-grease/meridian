"""Constructor-level tests for the SDK-backed runners.

Full HTTP-level coverage of the Anthropic and OpenAI SDKs would require
either pinning internal SDK implementation details or recording cassettes;
both are brittle. We instead verify that the runners instantiate with
sensible defaults and expose the expected ``provider`` / ``model_id``
attributes. Behavioral coverage of the shared retry + error-translation
paths lives in ``test_runner_ollama.py``.
"""
from __future__ import annotations

from drift_audit.runners.anthropic import AnthropicRunner
from drift_audit.runners.openai import OpenAIRunner


def test_anthropic_runner_constructs():
    r = AnthropicRunner("claude-opus-4-7", api_key="sk-dummy")
    assert r.provider == "anthropic"
    assert r.model_id == "claude-opus-4-7"
    assert r.client is not None


def test_openai_runner_constructs():
    r = OpenAIRunner("gpt-5-preview", api_key="sk-dummy")
    assert r.provider == "openai"
    assert r.model_id == "gpt-5-preview"
    assert r.client is not None
