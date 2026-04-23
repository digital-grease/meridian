"""End-to-end: seeded raw samples → emit snapshot → site build → verify
the gzip is published under ``/data/{week}/responses.jsonl.gz`` with a
correct SHA-256 in ``SHA256SUMS``.

Regression gate on the pipeline → site publication of raw responses.
Without this, a future change could break the emission step and the
site would quietly ship without the raw-data transparency artifact.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from meridian.corpus import load_corpus
from meridian.pipeline.manifest_writer import (
    RunnerDisplayInfo,
    build_manifest,
    write_manifest,
)
from meridian.pipeline.snapshot import emit_responses_snapshot
from meridian.runners.base import Sample
from meridian.storage import LocalSampleStore

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _sample(prompt_id: str, model_id: str, idx: int, text: str) -> Sample:
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


def test_responses_gz_published_alongside_metrics(tmp_path: Path):
    corpus = load_corpus()
    model_id = "fake-model-1"
    # Pick a small subset so the subprocess site build stays fast.
    prompts = corpus.public()[:2]

    store = LocalSampleStore(tmp_path / "raw")
    for p in prompts:
        for i in range(3):
            store.append("2026-W16", model_id, p.id,
                         _sample(p.id, model_id, i, "substantive answer"))

    manifest = build_manifest(
        store=store,
        corpus=corpus,
        week_id="2026-W16",
        history_weeks=0,
        display_info={
            model_id: RunnerDisplayInfo(
                model_id=model_id,
                display_name="Fake Model 1",
                provider="fake",
            ),
        },
        bootstrap_seed=1,
    )
    manifest_path = tmp_path / "manifest-2026-W16.json"
    write_manifest(manifest, [manifest_path])

    # Pipeline-side emission: the gzip must live at the canonical
    # repo-relative path the site build reads from.
    snapshot_dir = REPO_ROOT / "data" / "snapshots" / "2026-W16"
    snapshot_gz = snapshot_dir / "responses.jsonl.gz"
    # Respect any pre-existing file by backing it up; restore afterward.
    backup = None
    if snapshot_gz.exists():
        backup = snapshot_gz.read_bytes()
    try:
        report = emit_responses_snapshot(store, corpus, "2026-W16", snapshot_gz)
        assert report.sample_count == 6

        dist = tmp_path / "dist"
        result = subprocess.run(
            [
                "uv", "run", "python",
                str(REPO_ROOT / "site" / "src" / "build.py"),
                "--manifest", str(manifest_path),
                "--out", str(dist),
            ],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert result.returncode == 0, (
            f"site build failed: {result.stderr}\n{result.stdout}"
        )

        published = dist / "data" / "2026-W16" / "responses.jsonl.gz"
        assert published.exists(), \
            "responses.jsonl.gz was not published to /data/{week}/"

        # The published file is byte-identical to the pipeline output.
        assert published.read_bytes() == snapshot_gz.read_bytes()

        # SHA256SUMS lists it with a matching digest.
        sums_text = (dist / "data" / "2026-W16" / "SHA256SUMS").read_text()
        expected_digest = hashlib.sha256(published.read_bytes()).hexdigest()
        assert f"{expected_digest}  responses.jsonl.gz" in sums_text

        # Gunzip round-trips to 6 Sample records with the expected ids.
        with gzip.open(published, "rt", encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
        assert len(records) == 6
        assert {r["prompt_id"] for r in records} == {p.id for p in prompts}
    finally:
        # Clean up the snapshot we wrote into the real repo tree so the
        # test leaves no trace. Restore prior contents if we overwrote.
        if backup is not None:
            snapshot_gz.write_bytes(backup)
        elif snapshot_gz.exists():
            snapshot_gz.unlink()
            try:
                snapshot_dir.rmdir()
            except OSError:
                pass


def test_site_build_tolerates_missing_responses_gz(tmp_path: Path):
    """The site build must not fail when the pipeline hasn't emitted a
    snapshot yet — the default fixture case. A missing source is a
    no-op, not an error."""
    manifest_path = REPO_ROOT / "site" / "fixtures" / "manifest-2026-W16.json"
    dist = tmp_path / "dist"
    result = subprocess.run(
        [
            "uv", "run", "python",
            str(REPO_ROOT / "site" / "src" / "build.py"),
            "--manifest", str(manifest_path),
            "--out", str(dist),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0
    # No fixture-side raw data → no gzip, and SHA256SUMS doesn't mention it.
    sums_text = (dist / "data" / "2026-W16" / "SHA256SUMS").read_text()
    assert "responses.jsonl.gz" not in sums_text
