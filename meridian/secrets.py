"""Optional SSM Parameter Store-backed secret resolution.

The pipeline reads provider API keys from environment variables. On a
laptop that's fine — keys live in the shell. On the EC2 runner there is
no shell session to inherit from; secrets live in AWS SSM Parameter
Store as ``SecureString`` parameters and the wrapper script asks this
module to resolve them at startup.

Activation is opt-in: only ``MERIDIAN_SECRETS_SSM=1`` triggers SSM
lookups. Otherwise the function is a silent no-op and existing
environment variables pass through unchanged. The helper never
overwrites a key that is already set in the environment — this lets
ad-hoc local override (`ANTHROPIC_API_KEY=... uv run ...`) keep working
on the EC2 host too.

Environment-variable convention:

  MERIDIAN_SECRETS_SSM=1
  MERIDIAN_SECRETS_SSM_ANTHROPIC_PATH=/meridian/anthropic-api-key
  MERIDIAN_SECRETS_SSM_OPENAI_PATH=/meridian/openai-api-key

Each ``*_PATH`` is optional; if omitted, that key isn't fetched. boto3
is imported lazily so configurations that don't use SSM don't pay the
import cost.
"""
from __future__ import annotations

import logging
import os

_log = logging.getLogger(__name__)

# Map of (env-var the runner reads) → (env-var naming the SSM path).
_SECRET_PATHS: dict[str, str] = {
    "ANTHROPIC_API_KEY": "MERIDIAN_SECRETS_SSM_ANTHROPIC_PATH",
    "OPENAI_API_KEY": "MERIDIAN_SECRETS_SSM_OPENAI_PATH",
}


def resolve_ssm_secrets() -> None:
    """Populate provider-API-key environment variables from SSM, if enabled.

    Idempotent: skips any key already present in the environment so a
    locally-set key wins over the SSM value. Raises if SSM is enabled but
    a configured path is unreachable — silent failure here would mean
    sampling proceeds with an empty key and writes a manifest the user
    can't reproduce.
    """
    if os.environ.get("MERIDIAN_SECRETS_SSM") != "1":
        return

    pending: list[tuple[str, str]] = []
    for env_target, path_var in _SECRET_PATHS.items():
        if env_target in os.environ and os.environ[env_target]:
            continue
        ssm_path = os.environ.get(path_var)
        if not ssm_path:
            continue
        pending.append((env_target, ssm_path))

    if not pending:
        return

    # Lazy import — keeps the boto3 import cost off the hot path of every
    # CLI subcommand even though boto3 is a guaranteed hard dependency.
    import boto3  # noqa: PLC0415

    client = boto3.client("ssm")
    for env_target, ssm_path in pending:
        resp = client.get_parameter(Name=ssm_path, WithDecryption=True)
        os.environ[env_target] = resp["Parameter"]["Value"]
        _log.info("resolved %s from SSM path %s", env_target, ssm_path)
