"""Pipeline configuration loader.

Loads ``drift_audit/config.yaml`` (or a path passed on the CLI) describing:
  - which runners to include this week
  - sampling parameters (N, temperatures, concurrency)
  - where to store raw data

Provider API keys are NOT in config files. They are pulled from environment
variables at runtime. Putting secrets in version-controlled files is how
credentials leak.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

Provider = Literal["anthropic", "openai", "ollama"]

# A runner's schedule. v0.1 supports three patterns; cron expressions are
# a v0.2 concern if we ever need finer control than "every / even / odd".
Cadence = Literal["every_week", "even_weeks", "odd_weeks"]


class RunnerSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: Provider
    model_id: str
    base_url: str | None = None   # for ollama / self-hosted
    enabled: bool = True
    cadence: Cadence = "every_week"


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


class StorageSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    raw_dir: str = "data/raw"


class PipelineConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    sampling: SamplingSpec = Field(default_factory=SamplingSpec)
    storage: StorageSpec = Field(default_factory=StorageSpec)
    runners: list[RunnerSpec] = Field(default_factory=list)


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
    """
    from drift_audit.runners.anthropic import AnthropicRunner
    from drift_audit.runners.openai import OpenAIRunner
    from drift_audit.runners.ollama import OllamaRunner

    out = []
    for spec in config.runners:
        if not spec.enabled:
            continue
        if week_id is not None and not should_run_in_week(spec.cadence, week_id):
            continue
        if spec.provider == "anthropic":
            out.append(AnthropicRunner(spec.model_id, api_key=os.environ.get("ANTHROPIC_API_KEY")))
        elif spec.provider == "openai":
            out.append(OpenAIRunner(spec.model_id, api_key=os.environ.get("OPENAI_API_KEY")))
        elif spec.provider == "ollama":
            out.append(OllamaRunner(spec.model_id, base_url=spec.base_url or "http://localhost:11434"))
        else:  # pragma: no cover
            raise ValueError(f"unknown provider: {spec.provider}")
    return out
