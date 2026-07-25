"""Every published manifest must name the corpus that produced it.

CLAUDE.md requires any dashboard result be reproducible from the public
raw data within 5%, and the first thing needed to attempt that is which
prompt set was in force. The field existed from the start and was never
wired: `build_manifest` defaulted `corpus_git_sha` to the literal string
"unknown" and no caller ever passed anything, so all 13 published weeks
carry "unknown". These tests pin the wiring, not just the helper.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from meridian.corpus import corpus_git_sha, load_corpus
from meridian.pipeline.manifest_writer import build_manifest
from meridian.runners.base import Sample
from meridian.storage import LocalSampleStore

_SHA_LEN = 40


def _seed(tmp_path: Path, corpus) -> LocalSampleStore:
    store = LocalSampleStore(tmp_path)
    for p in corpus.public()[:2]:
        for i in range(12):
            store.append("2026-W16", "m1", p.id, Sample(
                prompt_id=p.id, model_id="m1", provider="fake",
                request_index=i, temperature=1.0, max_tokens=1024,
                text=f"answer {i}", model_version_string="m1-v",
                finish_reason="stop", latency_ms=1,
                captured_at=datetime.now(timezone.utc),
            ))
    return store


def test_detects_a_real_sha_in_this_checkout():
    sha = corpus_git_sha()
    assert sha != "unknown"
    assert len(sha.removesuffix("-dirty")) == _SHA_LEN


def test_sha_is_scoped_to_the_corpus_not_repo_head():
    """Repo HEAD moves on every unrelated commit. Citing it would imply
    the corpus changed when it did not."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=Path(__file__).parent,
    ).stdout.strip()
    corpus_sha = corpus_git_sha().removesuffix("-dirty")
    corpus_commit = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", "meridian/corpus/prompts.yaml"],
        capture_output=True, text=True,
        cwd=Path(__file__).resolve().parent.parent.parent,
    ).stdout.strip()
    assert corpus_sha == corpus_commit
    # Only meaningful while the corpus is not the most recent commit,
    # which is the normal state.
    if head != corpus_commit:
        assert corpus_sha != head


def test_unknown_outside_a_git_checkout(tmp_path: Path):
    """Must degrade honestly rather than raise or invent a value."""
    stray = tmp_path / "prompts.yaml"
    stray.write_text("schema_version: 1\ncorpus_version: x\nprompts: []\n")
    assert corpus_git_sha(stray) == "unknown"


def test_manifest_publishes_real_provenance_by_default(tmp_path: Path):
    """The regression guard that matters: build_manifest must produce a
    real sha and version without the caller remembering to pass them."""
    corpus = load_corpus()
    store = _seed(tmp_path, corpus)
    m = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=0, bootstrap_seed=1,
    )
    snap = m["snapshot"]
    assert snap["corpus_git_sha"] != "unknown"
    assert snap["corpus_version"] == corpus.corpus_version
    assert snap["corpus_version"].startswith("2026.")


def test_explicit_override_still_wins(tmp_path: Path):
    corpus = load_corpus()
    store = _seed(tmp_path, corpus)
    m = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=0, bootstrap_seed=1, corpus_git_sha="deadbeef",
    )
    assert m["snapshot"]["corpus_git_sha"] == "deadbeef"
