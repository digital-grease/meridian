"""End-to-end: FakeRunner -> orchestrator -> storage -> manifest_writer
-> site build subprocess -> verify generated HTML.

This is the single test that proves the whole stack hangs together.
It does not talk to any network; the FakeRunner returns canned text.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drift_audit.corpus import load_corpus
from drift_audit.pipeline.manifest_writer import (
    RunnerDisplayInfo,
    build_manifest,
    write_manifest,
)
from drift_audit.runners.base import Runner, Sample
from drift_audit.sampling.orchestrator import Orchestrator, SamplingPlan
from drift_audit.storage import LocalSampleStore

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class _FakeRunner(Runner):
    """Canned responses; deterministic refusal on refusal-boundary prompts."""
    provider = "fake"

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    async def sample(self, prompt, *, prompt_id, request_index, temperature, max_tokens=1024):
        is_boundary = prompt_id.startswith("ref-")
        text = (
            "I can't help with that request."
            if is_boundary
            else "This is a substantive answer. Reasonable people may disagree, but on balance, yes."
        )
        return Sample(
            prompt_id=prompt_id,
            model_id=self.model_id,
            provider=self.provider,
            request_index=request_index,
            temperature=temperature,
            max_tokens=max_tokens,
            text=text,
            model_version_string=f"{self.model_id}-2026-04-01",
            stop_reason="stop",
            latency_ms=1,
            captured_at=datetime.now(timezone.utc),
        )


@pytest.mark.asyncio
async def test_pipeline_produces_site_buildable_manifest(tmp_path: Path):
    corpus = load_corpus()
    # Limit to two prompts per axis to keep the test fast but representative.
    trimmed_prompts = []
    for axis in ("political", "scientific-consensus", "refusal-boundary"):
        trimmed_prompts.extend(corpus.by_axis(axis)[:2])

    store = LocalSampleStore(tmp_path / "raw")
    runner = _FakeRunner("fake-model-1")
    plan = SamplingPlan(
        week_id="2026-W16",
        n_default_temp=5,
        n_zero_temp=2,
        concurrency_per_provider=3,
    )
    orch = Orchestrator([runner], store, corpus, plan)
    outcome = await orch.run(prompts=trimmed_prompts)
    assert outcome.pairs_complete == len(trimmed_prompts)
    assert outcome.pairs_failed == 0

    manifest = build_manifest(
        store=store,
        corpus=corpus,
        week_id="2026-W16",
        display_info={
            "fake-model-1": RunnerDisplayInfo(
                model_id="fake-model-1",
                display_name="Fake Model 1",
                provider="fake",
            ),
        },
        bootstrap_seed=1,
    )

    manifest_path = tmp_path / "manifest-2026-W16.json"
    write_manifest(manifest, [manifest_path])
    assert manifest_path.exists()

    # Invoke the site builder as a subprocess — same interface the GitHub
    # Actions workflow uses.
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

    # A handful of required URLs should exist.
    for rel in (
        "index.html",
        "models/fake-model-1/index.html",
        "prompts/pol-abortion-legal/index.html",
        "data/2026-W16/metrics.csv",
        "sitemap.xml",
    ):
        assert (dist / rel).exists(), f"missing: {rel}"

    # Spot-check refusal rates match the fake data: boundary prompts -> 1.0,
    # others -> 0.0.
    data = json.loads(manifest_path.read_text())
    for m in data["metrics"]:
        if m["prompt_id"].startswith("ref-"):
            assert m["refusal_rate"] >= 0.9
        else:
            assert m["refusal_rate"] <= 0.1
