"""Prompt corpus loader.

The corpus is versioned YAML. This module loads and validates it into
Pydantic models the rest of the pipeline uses. The on-disk format is the
source of truth; runtime mutations are forbidden.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

Axis = Literal[
    "political",
    "historical-contested",
    "scientific-consensus",
    "refusal-boundary",
    "neutral-control",
    "factual-stability",
]


class Prompt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    axis: Axis
    title: str
    text: str
    held_out: bool = False
    description: str | None = None

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


class Corpus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(ge=1)
    corpus_version: str
    prompts: list[Prompt]

    def by_id(self, prompt_id: str) -> Prompt:
        for p in self.prompts:
            if p.id == prompt_id:
                return p
        raise KeyError(prompt_id)

    def by_axis(self, axis: Axis) -> list[Prompt]:
        return [p for p in self.prompts if p.axis == axis]

    def public(self) -> list[Prompt]:
        return [p for p in self.prompts if not p.held_out]

    def held_out(self) -> list[Prompt]:
        return [p for p in self.prompts if p.held_out]


_DEFAULT_PATH = Path(__file__).resolve().parent / "prompts.yaml"


def load_corpus(path: Path | None = None) -> Corpus:
    """Load and validate the corpus from YAML.

    Duplicates in ``id`` are a hard error; axes must match the site's
    schema.py ``Axis`` literal; ``schema_version`` is checked but not
    mapped — migrations are a v0.2 concern.
    """
    p = path or _DEFAULT_PATH
    data = yaml.safe_load(p.read_text())
    corpus = Corpus.model_validate(data)

    seen: set[str] = set()
    for prompt in corpus.prompts:
        if prompt.id in seen:
            raise ValueError(f"duplicate prompt id: {prompt.id}")
        seen.add(prompt.id)

    return corpus
