"""Silent-update-warning rendering regression gate.

``build_manifest`` populates ``silent_update_warnings`` on every
manifest. This test proves the rendered site actually displays them —
guarding against the same orphaning pattern that motivated the Phase-1
closeout (an analysis output that lands in the manifest but is invisible
to readers).
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from meridian.corpus import load_corpus
from meridian.pipeline.manifest_writer import (
    RunnerDisplayInfo,
    build_manifest,
    write_manifest,
)
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
        model_version_string=f"{model_id}-2026-04-01",
        stop_reason="stop",
        latency_ms=1,
        captured_at=datetime(2026, 4, 19, tzinfo=timezone.utc),
    )


def test_silent_update_warning_rendered_on_index(tmp_path: Path):
    corpus = load_corpus()
    model_id = "fake-model-1"
    # A neutral-control prompt flipping from answer to refusal
    # week-over-week is the textbook silent-update signal.
    neutrals = [p for p in corpus.public() if p.axis == "neutral-control"]
    assert neutrals, "corpus must have at least one neutral-control prompt"
    neutral = neutrals[0]

    store = LocalSampleStore(tmp_path / "raw")
    for i in range(15):
        store.append(
            "2026-W15", model_id, neutral.id,
            _sample(neutral.id, model_id, i, "The capital is Paris."),
        )
    for i in range(15):
        store.append(
            "2026-W16", model_id, neutral.id,
            _sample(neutral.id, model_id, i, "I can't help with that request."),
        )

    manifest = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=4, bootstrap_seed=1,
        display_info={
            model_id: RunnerDisplayInfo(
                model_id=model_id,
                display_name="Fake Model 1",
                provider="fake",
            ),
        },
    )
    assert manifest["silent_update_warnings"], \
        "scenario did not produce a silent-update warning"

    manifest_path = tmp_path / "manifest-2026-W16.json"
    write_manifest(manifest, [manifest_path])

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

    index_html = (dist / "index.html").read_text()
    assert "silent-updates-heading" in index_html, \
        "silent-update section missing from index.html"
    assert "Silent-update candidates this week" in index_html
    # Model linked through (fallback would be <code> tag).
    assert f'href="/models/{model_id}/"' in index_html
    # Severity badge rendered.
    assert 'class="severity severity-' in index_html


def test_silent_update_section_absent_when_no_warnings(tmp_path: Path):
    corpus = load_corpus()
    model_id = "fake-model-1"
    stable = [p for p in corpus.public() if p.axis == "neutral-control"][:1]
    assert stable

    store = LocalSampleStore(tmp_path / "raw")
    for week in ("2026-W15", "2026-W16"):
        for i in range(15):
            store.append(
                week, model_id, stable[0].id,
                _sample(stable[0].id, model_id, i, "The capital is Paris."),
            )

    manifest = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=4, bootstrap_seed=1,
    )
    assert not manifest["silent_update_warnings"]

    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest, [manifest_path])
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
    index_html = (dist / "index.html").read_text()
    assert "silent-updates-heading" not in index_html, \
        "silent-update section should not render when warnings are empty"
