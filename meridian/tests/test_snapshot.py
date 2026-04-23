"""Tests for meridian.pipeline.snapshot.

Held-out exclusion is the single hardest behavioural requirement: the
emitted gzip must never carry a held-out sample. The test constructs a
corpus with both kinds of prompts, seeds samples for all of them, and
checks that only public ids round-trip.
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from meridian.corpus import Corpus, Prompt
from meridian.pipeline.snapshot import emit_responses_snapshot, snapshot_path
from meridian.runners.base import Sample
from meridian.storage import LocalSampleStore


def _sample(prompt_id: str, model_id: str, idx: int, text: str = "a") -> Sample:
    return Sample(
        prompt_id=prompt_id,
        model_id=model_id,
        provider="fake",
        request_index=idx,
        temperature=1.0,
        max_tokens=1024,
        text=text,
        model_version_string=f"{model_id}-2026-04-20",
        stop_reason="stop",
        latency_ms=1,
        captured_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )


def _corpus_with(public_ids: list[str], held_out_ids: list[str]) -> Corpus:
    prompts: list[Prompt] = []
    for pid in public_ids:
        prompts.append(Prompt(id=pid, axis="political", title=pid, text="x", held_out=False))
    for pid in held_out_ids:
        prompts.append(Prompt(id=pid, axis="political", title=pid, text="x", held_out=True))
    return Corpus(schema_version=1, corpus_version="test", prompts=prompts)


def test_emit_writes_public_samples_only(tmp_path: Path):
    corpus = _corpus_with(["pub-1", "pub-2"], ["hout-1"])
    store = LocalSampleStore(tmp_path / "raw")
    # Seed 3 samples per prompt per model across 2 models.
    for pid in ("pub-1", "pub-2", "hout-1"):
        for mid in ("m-a", "m-b"):
            for i in range(3):
                store.append("2026-W17", mid, pid, _sample(pid, mid, i))

    out = tmp_path / "responses.jsonl.gz"
    report = emit_responses_snapshot(store, corpus, "2026-W17", out)

    # 2 public prompts × 2 models × 3 samples = 12 records, zero held-out.
    assert report.sample_count == 12
    assert report.prompts_included == 4
    assert report.prompts_skipped_held_out == 2
    assert out.exists()

    with gzip.open(out, "rt", encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    assert len(records) == 12
    assert {r["prompt_id"] for r in records} == {"pub-1", "pub-2"}
    assert all(r["prompt_id"] != "hout-1" for r in records)


def test_emit_noop_when_week_absent(tmp_path: Path):
    corpus = _corpus_with(["pub-1"], [])
    store = LocalSampleStore(tmp_path / "raw")
    out = tmp_path / "r.jsonl.gz"
    report = emit_responses_snapshot(store, corpus, "2026-W99", out)
    assert report.sample_count == 0
    assert report.prompts_included == 0
    # File is still created (empty gzip) so downstream SHA256SUMS
    # always has a checksum to list.
    assert out.exists()


def test_emit_skips_prompts_not_in_corpus(tmp_path: Path):
    """Defensive check: a prompt id present in storage but missing from
    the currently-loaded corpus (e.g. rotated out) is skipped, not
    published unadvertised."""
    corpus = _corpus_with(["pub-1"], [])
    store = LocalSampleStore(tmp_path / "raw")
    store.append("2026-W17", "m-a", "pub-1", _sample("pub-1", "m-a", 0))
    store.append("2026-W17", "m-a", "ghost-prompt", _sample("ghost-prompt", "m-a", 0))

    out = tmp_path / "r.jsonl.gz"
    report = emit_responses_snapshot(store, corpus, "2026-W17", out)
    assert report.sample_count == 1

    with gzip.open(out, "rt", encoding="utf-8") as f:
        pids = {json.loads(line)["prompt_id"] for line in f if line.strip()}
    assert pids == {"pub-1"}


def test_snapshot_path_is_canonical(tmp_path: Path):
    p = snapshot_path(tmp_path, "2026-W17")
    assert p == tmp_path / "data" / "snapshots" / "2026-W17" / "responses.jsonl.gz"
