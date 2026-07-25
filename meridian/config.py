"""Pipeline configuration loader.

Loads ``meridian/config.yaml`` (or a path passed on the CLI) describing:
  - which runners to include this week
  - sampling parameters (N, temperatures, concurrency)
  - where to store raw data

Provider API keys are NOT in config files. They are pulled from environment
variables at runtime. Putting secrets in version-controlled files is how
credentials leak.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

Provider = Literal["anthropic", "openai", "ollama"]

# A runner's schedule. v0.1 supports three patterns; cron expressions are
# a v0.2 concern if we ever need finer control than "every / even / odd".
Cadence = Literal["every_week", "even_weeks", "odd_weeks"]

_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")


class RunnerSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: Provider
    model_id: str
    base_url: str | None = None   # for ollama / self-hosted
    enabled: bool = True
    cadence: Cadence = "every_week"
    # Optional per-runner completion cap, overriding sampling.max_tokens.
    # Reasoning-default models bill reasoning tokens against the same
    # budget as visible output, so the shared cap that is generous for a
    # non-reasoning model can be spent entirely on reasoning, returning
    # an empty completion with finish_reason="length". Raise it for
    # those models rather than raising it for everyone, which would
    # inflate cost on the models that do not need it.
    max_tokens: int | None = Field(default=None, gt=0)
    # Optional sha256 digest for control-group invariance. Currently
    # honoured by the ollama runner: on startup the runner queries
    # /api/tags and refuses to sample if the served digest doesn't
    # match. Bare 64-char hex (no "sha256:" prefix) — that's the form
    # ollama's API emits.
    digest: str | None = None

    @field_validator("digest")
    @classmethod
    def _validate_digest(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _DIGEST_RE.match(v):
            raise ValueError(
                "digest must be a bare 64-char lowercase hex sha256 "
                "(no 'sha256:' prefix); got " + repr(v)
            )
        return v


def should_run_in_week(cadence: Cadence, week_id: str) -> bool:
    """Does a runner with ``cadence`` belong in the run for ``week_id``?

    ``week_id`` is an ISO format string like ``"2026-W16"``.
    """
    if cadence == "every_week":
        return True
    try:
        week_num = int(week_id.rsplit("-W", 1)[1])
    except (IndexError, ValueError) as e:
        raise ValueError(f"unparseable week_id: {week_id!r}") from e
    if cadence == "even_weeks":
        return week_num % 2 == 0
    if cadence == "odd_weeks":
        return week_num % 2 == 1
    raise ValueError(f"unknown cadence: {cadence!r}")


class SamplingSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    n_default_temp: int = 20
    n_zero_temp: int = 5
    default_temperature: float = 1.0
    zero_temperature: float = 0.0
    max_tokens: int = 1024
    concurrency_per_provider: int = 4


class S3StorageSpec(BaseModel):
    """Optional S3 archival target for raw samples and published manifests.

    When absent from the config, S3 upload is disabled entirely. AWS
    credentials come from the environment via boto3's default provider
    chain (env vars, instance profile, OIDC, etc.) — never from this
    file. Terraform module for the target bucket lives at
    ``infra/terraform/s3``.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    bucket: str
    region: str | None = None          # boto3 falls back to AWS_REGION env
    prefix: str = ""                   # e.g. "meridian/" to namespace
    publish_latest_pointer: bool = True  # write manifests/latest.json


class StorageSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    raw_dir: str = "data/raw"
    s3: S3StorageSpec | None = None


class StanceSpec(BaseModel):
    """Stance classifier wiring.

    The classifier itself is an LLM call (currently a Haiku 4.5
    invocation per response). When `enabled=False`, every metric
    record's stance is "na" — the project's stance signal is dark.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = False
    provider: Provider = "anthropic"
    model_id: str = "claude-haiku-4-5-20251001"
    cache_path: str = "data/cache/stance/cache.jsonl"


class EmbeddingSpec(BaseModel):
    """Embedding-centroid drift wiring.

    Disabled by default: sentence-transformers' default model is a
    ~400 MB download, and CI cold-start cost has not been measured.
    Enable locally to populate `embedding_centroid_shift` on every
    metric record.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = False
    model: str = "sentence-transformers/all-mpnet-base-v2"


class PipelineConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    sampling: SamplingSpec = Field(default_factory=SamplingSpec)
    storage: StorageSpec = Field(default_factory=StorageSpec)
    runners: list[RunnerSpec] = Field(default_factory=list)
    stance: StanceSpec = Field(default_factory=StanceSpec)
    embedding: EmbeddingSpec = Field(default_factory=EmbeddingSpec)


_DEFAULT_PATH = Path(__file__).resolve().parent / "config.yaml"


def load_config(path: Path | None = None) -> PipelineConfig:
    p = path or _DEFAULT_PATH
    if not p.exists():
        return PipelineConfig()
    data = yaml.safe_load(p.read_text()) or {}
    return PipelineConfig.model_validate(data)


def build_runners(config: PipelineConfig, *, week_id: str | None = None):
    """Instantiate enabled runners from config.

    If ``week_id`` is given, runners whose cadence excludes that week are
    also filtered out. Passing ``None`` disables cadence filtering and
    returns every enabled runner (useful for the ``estimate`` subcommand's
    "monthly average" view).

    ``MERIDIAN_SKIP_PROVIDERS`` (env var, comma-separated provider names)
    excludes matching runners after the config-level ``enabled`` check.
    The intended caller is CI, which has no local Ollama server: set
    ``MERIDIAN_SKIP_PROVIDERS=ollama`` in the workflow so the local
    config.yaml's ``ollama: enabled: true`` still works for interactive
    runs on the maintainer's machine.
    """
    from meridian.runners.anthropic import AnthropicRunner
    from meridian.runners.openai import OpenAIRunner
    from meridian.runners.ollama import OllamaRunner

    skip_env = os.environ.get("MERIDIAN_SKIP_PROVIDERS", "").strip()
    skip = {s.strip().lower() for s in skip_env.split(",") if s.strip()}

    out = []
    for spec in config.runners:
        if not spec.enabled:
            continue
        if spec.provider.lower() in skip:
            print(
                f"[meridian] skipping {spec.provider}/{spec.model_id} "
                f"(MERIDIAN_SKIP_PROVIDERS)",
                file=sys.stderr,
            )
            continue
        if week_id is not None and not should_run_in_week(spec.cadence, week_id):
            continue
        if spec.provider == "anthropic":
            out.append(AnthropicRunner(
                spec.model_id,
                api_key=os.environ.get("ANTHROPIC_API_KEY"),
                max_tokens=spec.max_tokens,
            ))
        elif spec.provider == "openai":
            out.append(OpenAIRunner(
                spec.model_id,
                api_key=os.environ.get("OPENAI_API_KEY"),
                max_tokens=spec.max_tokens,
            ))
        elif spec.provider == "ollama":
            out.append(OllamaRunner(
                spec.model_id,
                base_url=spec.base_url or "http://localhost:11434",
                expected_digest=spec.digest,
                max_tokens=spec.max_tokens,
            ))
        else:  # pragma: no cover
            raise ValueError(f"unknown provider: {spec.provider}")
    return out
