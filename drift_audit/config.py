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


class RunnerSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: Provider
    model_id: str
    base_url: str | None = None   # for ollama / self-hosted
    enabled: bool = True


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


def build_runners(config: PipelineConfig):
    """Instantiate runners from config, pulling API keys from env."""
    from drift_audit.runners.anthropic import AnthropicRunner
    from drift_audit.runners.openai import OpenAIRunner
    from drift_audit.runners.ollama import OllamaRunner

    out = []
    for spec in config.runners:
        if not spec.enabled:
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
