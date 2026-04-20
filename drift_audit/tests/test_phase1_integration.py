"""Phase-1 closeout regression gate.

Verifies that every Phase-1 analysis output reaches the artifacts a
reader of the public site actually sees. Without this test, a future
change could drop stance / BH / embedding rendering on the dashboard
and all other tests would still pass — that is exactly the orphaning
that motivated this phase (see
``.devloop/spikes/next-work-2026-04-19.md``).

The test runs the full pipeline:

  seeded two-week storage
    -> build_manifest (with stance_by_key + fake EmbeddingModel)
    -> write_manifest
    -> subprocess call to site/src/build.py
    -> scrape rendered model.html and prompt.html

and asserts that each Phase-1 signal is visibly present.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from drift_audit.analysis.stance import StanceResult
from drift_audit.corpus import load_corpus
from drift_audit.pipeline.manifest_writer import (
    RunnerDisplayInfo,
    build_manifest,
    write_manifest,
)
from drift_audit.runners.base import Sample
from drift_audit.storage import LocalSampleStore

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class _DeterministicEmbedder:
    """Fake embedding model that assigns stable direction by text content.

    Returns a 4-dim vector that depends only on the first word of the
    text, so current vs prior centroids differ exactly when the
    response content changed. No heavy deps.
    """
    def encode(self, texts: list[str]):
        import numpy as np
        vectors = []
        for t in texts:
            first = (t.split() or [""])[0].lower()
            if first.startswith("i"):
                vectors.append([1.0, 0.0, 0.0, 0.0])
            elif first.startswith("this"):
                vectors.append([0.0, 1.0, 0.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0, 0.0])
        return np.asarray(vectors, dtype=float)


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


def test_all_phase1_signals_reach_rendered_html(tmp_path: Path):
    corpus = load_corpus()
    model_id = "fake-model-1"
    political = corpus.by_axis("political")[:3]
    # Two shifted prompts exercise distinct Phase-1 signals:
    #   recent_shift — flips at the current week, drives BH significance.
    #   older_shift  — flipped weeks ago, drives change-point detection
    #                  (PELT's min_size=2 excludes regime changes at the
    #                  very last series position).
    recent_shift = political[0]
    older_shift = political[1]

    store = LocalSampleStore(tmp_path / "raw")
    weeks = [f"2026-W{n:02d}" for n in range(11, 17)]
    current_week = "2026-W16"
    older_flip_start = "2026-W14"
    for week_id in weeks:
        for prompt in political:
            flipped = (
                (prompt.id == recent_shift.id and week_id == current_week)
                or (prompt.id == older_shift.id and week_id >= older_flip_start)
            )
            for i in range(15):
                text = (
                    "I can't help with that request."
                    if flipped
                    else "This is a substantive answer without refusals."
                )
                store.append(week_id, model_id, prompt.id,
                             _sample(prompt.id, model_id, i, text))

    # Every political prompt is stance-bearing; assign a scripted
    # stance result so stance is populated on the current week.
    stance_by_key = {
        (prompt.id, model_id): StanceResult(stance="neutral", confidence=0.85)
        for prompt in political
    }

    manifest = build_manifest(
        store=store,
        corpus=corpus,
        week_id="2026-W16",
        history_weeks=8,
        display_info={
            model_id: RunnerDisplayInfo(
                model_id=model_id,
                display_name="Fake Model 1",
                provider="fake",
            ),
        },
        bootstrap_seed=7,
        stance_by_key=stance_by_key,
        embedding_model=_DeterministicEmbedder(),
    )

    manifest_path = tmp_path / "manifest-2026-W16.json"
    write_manifest(manifest, [manifest_path])

    # Sanity-check the manifest before invoking the site builder.
    raw = json.loads(manifest_path.read_text())
    by_prompt = {m["prompt_id"]: m for m in raw["metrics"]}

    recent = by_prompt[recent_shift.id]
    assert recent["refusal_drift"] is not None, \
        "drift test not populated on recent-shift prompt"
    assert recent["refusal_drift"]["significant_after_bh"] is True, \
        "BH correction did not mark the 0→1 refusal flip as significant"
    assert recent["stance"] == "neutral"
    assert recent["embedding_centroid_shift"] is not None
    assert recent["embedding_centroid_shift"] > 0.0, \
        "expected non-zero semantic drift for the flipped prompt"

    older = by_prompt[older_shift.id]
    assert older["change_points"]["refusal_rate"], \
        "change-point indices missing on older-shift prompt"

    # Build the site.
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

    prompt_html = (dist / "prompts" / recent_shift.id / "index.html").read_text()
    older_prompt_html = (dist / "prompts" / older_shift.id / "index.html").read_text()
    model_html = (dist / "models" / model_id / "index.html").read_text()

    # 1. Stance badge rendered on both pages.
    for name, html in [("prompt.html", prompt_html), ("model.html", model_html)]:
        assert 'class="stance stance-neutral">neutral<' in html, \
            f"stance badge missing from {name}"

    # 2. Semantic-drift cell rendered on both pages.
    for name, html in [("prompt.html", prompt_html), ("model.html", model_html)]:
        assert "semantic-drift-cell" in html, \
            f"semantic drift cell missing from {name}"
        assert "Semantic drift" in html, \
            f"semantic drift section heading missing from {name}"

    # 3. Change-point markers appear in the older-shift prompt's
    #    sparkline SVG and on the model page. chart.sparkline() appends
    #    " (change-point marked)" to the aria-label whenever it emits
    #    at least one marker.
    assert "(change-point marked)" in older_prompt_html, \
        "change-point sparkline markers missing from older-shift prompt.html"
    assert "(change-point marked)" in model_html, \
        "change-point sparkline markers missing from model.html"
