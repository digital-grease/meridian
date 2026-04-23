#!/usr/bin/env python3
"""Auto-generate the corpus changelog from git history.

Walks commits that touched ``meridian/corpus/prompts.yaml`` and
emits a Markdown log: date, commit SHA, added/modified prompt IDs,
author. Committed output lives at ``meridian/corpus/CHANGELOG.md``.

Run manually before a release or from CI on a schedule:

    uv run python scripts/corpus_changelog.py > meridian/corpus/CHANGELOG.md
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORPUS_FILE = REPO / "meridian" / "corpus" / "prompts.yaml"

_ID_LINE_RE = re.compile(r"^\+\s*-\s*id:\s*(\S+)")


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(REPO), *args], text=True)


def _commits_touching(path: Path) -> list[str]:
    out = _git("log", "--pretty=format:%H", "--", str(path.relative_to(REPO)))
    return [line for line in out.splitlines() if line]


def _meta(sha: str) -> tuple[str, str, str]:
    # date  |  author  |  subject
    line = _git(
        "show", "-s", "--format=%ad|%an|%s", "--date=short", sha
    ).strip()
    date, author, subject = line.split("|", 2)
    return date, author, subject


def _added_ids_in_commit(sha: str, rel_path: str) -> list[str]:
    """Prompt IDs that appeared in this commit's diff of the corpus file."""
    try:
        diff = _git("show", "--format=", "--unified=0", sha, "--", rel_path)
    except subprocess.CalledProcessError:
        return []
    out: list[str] = []
    for line in diff.splitlines():
        m = _ID_LINE_RE.match(line)
        if m:
            out.append(m.group(1))
    return out


def main() -> int:
    if not CORPUS_FILE.exists():
        print("corpus file not found", file=sys.stderr)
        return 2
    rel = CORPUS_FILE.relative_to(REPO).as_posix()

    print("# Corpus changelog\n")
    print(
        "Auto-generated from the git history of `meridian/corpus/prompts.yaml`. "
        "Prompts are never edited in place; a revision supersedes the prior id. "
        "Regenerate with `scripts/corpus_changelog.py`.\n"
    )

    for sha in _commits_touching(CORPUS_FILE):
        date, author, subject = _meta(sha)
        added = _added_ids_in_commit(sha, rel)
        short_sha = sha[:7]
        print(f"## {date} — `{short_sha}` — {subject}")
        print(f"*Author: {author}*\n")
        if added:
            print("Added / modified:")
            for pid in added:
                print(f"- `{pid}`")
        else:
            print("*No prompt id changes in this commit.*")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
