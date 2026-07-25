"""Prompt corpus loader.

The corpus is versioned YAML. This module loads and validates it into
Pydantic models the rest of the pipeline uses. The on-disk format is the
source of truth; runtime mutations are forbidden.
"""
from __future__ import annotations

import hashlib
import subprocess
from functools import lru_cache
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

    def all(self) -> list[Prompt]:
        return list(self.prompts)

    @property
    def has_held_out(self) -> bool:
        return any(p.held_out for p in self.prompts)


_CORPUS_DIR = Path(__file__).resolve().parent
_DEFAULT_PATH = _CORPUS_DIR / "prompts.yaml"
# Searched in order. Both paths are gitignored so the held-out set never
# lands in public git history. ``.local.yaml`` is the preferred filename
# for single-machine maintainer copies.
_DEFAULT_HELD_OUT_PATHS: tuple[Path, ...] = (
    _CORPUS_DIR / "held_out.local.yaml",
    _CORPUS_DIR / "held_out.yaml",
)


@lru_cache(maxsize=4)
def corpus_git_sha(path: Path | None = None) -> str:
    """Commit that last changed the public corpus file, or ``"unknown"``.

    Published in every manifest's snapshot so a reader can pin the exact
    prompt set behind a number. CLAUDE.md requires any dashboard result
    be reproducible from the public raw data, and "which corpus produced
    this" is the first thing needed to try.

    Scoped to the corpus file rather than repo HEAD deliberately: HEAD
    moves on every unrelated code change, so citing it would imply the
    corpus changed when it did not. This value is stable until the
    prompts actually change.

    Only the *public* corpus is hashed. The held-out files are gitignored
    by design (see :data:`_DEFAULT_HELD_OUT_PATHS`), so they have no
    commit to point at, and publishing one would leak their existence
    schedule anyway.

    Returns ``"unknown"`` when git is unavailable or the tree is not a
    repository, and suffixes ``-dirty`` when the file has uncommitted
    edits — a sha that silently ignored local modifications would be a
    false provenance claim, which is worse than admitting ignorance.
    """
    target = path or _DEFAULT_PATH
    try:
        sha = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", target.name],
            cwd=target.parent, capture_output=True, text=True, timeout=10,
        )
        if sha.returncode != 0 or not sha.stdout.strip():
            return "unknown"
        rev = sha.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", target.name],
            cwd=target.parent, capture_output=True, text=True, timeout=10,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            return f"{rev}-dirty"
        return rev
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _load_prompts_file(path: Path, force_held_out: bool) -> list[Prompt]:
    data = yaml.safe_load(path.read_text()) or {}
    prompts: list[Prompt] = []
    for raw in data.get("prompts", []) or []:
        # Held-out files override any held_out flag in the YAML so a
        # misplaced ``held_out: false`` cannot cause a leak.
        if force_held_out:
            raw = {**raw, "held_out": True}
        prompts.append(Prompt.model_validate(raw))
    return prompts


def load_corpus(
    path: Path | None = None,
    *,
    held_out_path: Path | None = None,
) -> Corpus:
    """Load and validate the corpus from YAML.

    Args:
        path: public prompts file. Defaults to ``prompts.yaml`` in this dir.
        held_out_path: optional held-out prompts file. If omitted, the
            default held-out paths are probed and the first existing file is
            used. If none exist, the corpus ships without a held-out set
            (valid — the held-out comparison simply produces no output).

    Held-out prompts are force-flagged ``held_out=True`` regardless of what
    the YAML says, so the flag can never accidentally be false in storage.
    """
    public_path = path or _DEFAULT_PATH
    data = yaml.safe_load(public_path.read_text()) or {}
    public_prompts = _load_prompts_file(public_path, force_held_out=False)

    held_out_prompts: list[Prompt] = []
    if held_out_path is not None:
        if held_out_path.exists():
            held_out_prompts = _load_prompts_file(held_out_path, force_held_out=True)
    else:
        for candidate in _DEFAULT_HELD_OUT_PATHS:
            if candidate.exists():
                held_out_prompts = _load_prompts_file(candidate, force_held_out=True)
                break

    all_prompts = public_prompts + held_out_prompts

    seen: set[str] = set()
    for prompt in all_prompts:
        if prompt.id in seen:
            raise ValueError(f"duplicate prompt id: {prompt.id}")
        seen.add(prompt.id)

    corpus = Corpus(
        schema_version=data["schema_version"],
        corpus_version=data["corpus_version"],
        prompts=all_prompts,
    )
    return corpus
