"""Tests for the optional SSM-backed secrets resolver."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from meridian import secrets


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # Strip every variable the resolver reads or writes so each test
    # starts from a known-empty baseline.
    for k in (
        "MERIDIAN_SECRETS_SSM",
        "MERIDIAN_SECRETS_SSM_ANTHROPIC_PATH",
        "MERIDIAN_SECRETS_SSM_OPENAI_PATH",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)


def test_disabled_is_noop():
    # MERIDIAN_SECRETS_SSM not set → no SSM calls at all.
    with patch("boto3.client") as mock_client:
        secrets.resolve_ssm_secrets()
    mock_client.assert_not_called()


def test_enabled_with_no_paths_is_noop(monkeypatch):
    monkeypatch.setenv("MERIDIAN_SECRETS_SSM", "1")
    with patch("boto3.client") as mock_client:
        secrets.resolve_ssm_secrets()
    mock_client.assert_not_called()


def test_enabled_fetches_configured_paths(monkeypatch):
    monkeypatch.setenv("MERIDIAN_SECRETS_SSM", "1")
    monkeypatch.setenv("MERIDIAN_SECRETS_SSM_ANTHROPIC_PATH", "/meridian/anthropic")
    monkeypatch.setenv("MERIDIAN_SECRETS_SSM_OPENAI_PATH", "/meridian/openai")

    fake_responses = {
        "/meridian/anthropic": {"Parameter": {"Value": "sk-ant-from-ssm"}},
        "/meridian/openai": {"Parameter": {"Value": "sk-openai-from-ssm"}},
    }

    class FakeSSM:
        def get_parameter(self, *, Name, WithDecryption):
            assert WithDecryption is True
            return fake_responses[Name]

    with patch("boto3.client", return_value=FakeSSM()):
        secrets.resolve_ssm_secrets()

    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-ssm"
    assert os.environ["OPENAI_API_KEY"] == "sk-openai-from-ssm"


def test_existing_env_var_wins_over_ssm(monkeypatch):
    monkeypatch.setenv("MERIDIAN_SECRETS_SSM", "1")
    monkeypatch.setenv("MERIDIAN_SECRETS_SSM_ANTHROPIC_PATH", "/meridian/anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-shell")

    with patch("boto3.client") as mock_client:
        secrets.resolve_ssm_secrets()

    # Should not have called SSM at all because the only configured path
    # was for ANTHROPIC_API_KEY which is already set.
    mock_client.assert_not_called()
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-shell"


def test_partial_config_fetches_only_configured(monkeypatch):
    monkeypatch.setenv("MERIDIAN_SECRETS_SSM", "1")
    monkeypatch.setenv("MERIDIAN_SECRETS_SSM_OPENAI_PATH", "/meridian/openai")
    # Anthropic path intentionally not set — the runner-build step will
    # fall through to whatever ANTHROPIC_API_KEY env var was there (here
    # nothing) and the runner construction itself will fail later if it
    # actually needs that key. That's the existing contract; this helper
    # doesn't second-guess it.

    class FakeSSM:
        def get_parameter(self, *, Name, WithDecryption):
            assert Name == "/meridian/openai"
            return {"Parameter": {"Value": "sk-openai"}}

    with patch("boto3.client", return_value=FakeSSM()):
        secrets.resolve_ssm_secrets()

    import os
    assert os.environ["OPENAI_API_KEY"] == "sk-openai"
    assert "ANTHROPIC_API_KEY" not in os.environ
