"""Held-out protocol tests: loader, manifest exclusion, site leak defense,
comparison analyzer."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from meridian.analysis.holdout_compare import compare_holdout
from meridian.corpus import load_corpus
from meridian.corpus.corpus import Prompt
from meridian.pipeline.manifest_writer import build_manifest, write_manifest
from meridian.runners.base import Sample
from meridian.storage import LocalSampleStore

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# --------------- Loader ---------------

def test_corpus_without_heldout_is_all_public():
    corpus = load_corpus()
    # Default repo has no held_out.yaml checked in.
    assert corpus.held_out() == []
    assert corpus.has_held_out is False


def test_corpus_with_heldout_file_merges(tmp_path: Path):
    held_out = {
        "schema_version": 1,
        "corpus_version": "test",
        "prompts": [
            {"id": "hout-test-01", "axis": "political",
             "title": "t1", "text": "held-out text one"},
            {"id": "hout-test-02", "axis": "scientific-consensus",
             "title": "t2", "text": "held-out text two",
             "held_out": False},  # will be force-flagged True
        ],
    }
    p = tmp_path / "ho.yaml"
    p.write_text(yaml.safe_dump(held_out))

    corpus = load_corpus(held_out_path=p)
    assert corpus.has_held_out
    held = corpus.held_out()
    assert {h.id for h in held} == {"hout-test-01", "hout-test-02"}
    # Force-flag: even the one with held_out=False in YAML is now held_out.
    assert all(h.held_out for h in held)
    # Public count unchanged by a held-out merge.
    assert len(corpus.public()) == len(load_corpus().public())


def test_corpus_rejects_duplicate_ids_across_files(tmp_path: Path):
    held_out = {
        "schema_version": 1, "corpus_version": "test",
        "prompts": [
            {"id": "pol-abortion-legal",  # collision with public corpus
             "axis": "political", "title": "x", "text": "x"},
        ],
    }
    p = tmp_path / "ho.yaml"
    p.write_text(yaml.safe_dump(held_out))
    with pytest.raises(ValueError, match="duplicate"):
        load_corpus(held_out_path=p)


# --------------- Manifest writer strict exclusion ---------------

def _fake_sample(**kw) -> Sample:
    return Sample(
        prompt_id=kw["prompt_id"], model_id=kw["model_id"],
        provider="fake",
        request_index=kw.get("idx", 0),
        temperature=1.0, max_tokens=1024,
        text=kw.get("text", "ok"),
        model_version_string=kw["model_id"] + "-v",
        stop_reason="stop",
        latency_ms=1,
        captured_at=datetime(2026, 4, 19, tzinfo=timezone.utc),
    )


def _seed_with_heldout(tmp_path: Path) -> tuple[LocalSampleStore, Path]:
    """Return (store, held_out_path). Seeds 2 public + 2 held-out prompts."""
    held_out = {
        "schema_version": 1, "corpus_version": "test",
        "prompts": [
            {"id": "hout-test-pol", "axis": "political",
             "title": "ho-pol", "text": "held political probe"},
            {"id": "hout-test-sci", "axis": "scientific-consensus",
             "title": "ho-sci", "text": "held sci probe"},
        ],
    }
    ho_path = tmp_path / "ho.yaml"
    ho_path.write_text(yaml.safe_dump(held_out))

    corpus = load_corpus(held_out_path=ho_path)
    store = LocalSampleStore(tmp_path / "raw")
    week = "2026-W16"
    model = "fake-1"
    for prompt_id in [
        "pol-abortion-legal", "sci-vaccines-safety",
        "hout-test-pol", "hout-test-sci",
    ]:
        for i in range(3):
            is_refusal = prompt_id.startswith("ref-")
            store.append(week, model, prompt_id, _fake_sample(
                prompt_id=prompt_id, model_id=model, idx=i,
                text=("I can't help" if is_refusal else "substantive answer"),
            ))
    return store, ho_path


def test_public_manifest_excludes_held_out(tmp_path: Path):
    store, ho_path = _seed_with_heldout(tmp_path)
    corpus = load_corpus(held_out_path=ho_path)
    mf = build_manifest(store=store, corpus=corpus, week_id="2026-W16",
                        history_weeks=0, bootstrap_seed=1)
    ids_in_prompts = {p["prompt_id"] for p in mf["prompts"]}
    assert "hout-test-pol" not in ids_in_prompts
    assert "hout-test-sci" not in ids_in_prompts
    ids_in_metrics = {m["prompt_id"] for m in mf["metrics"]}
    assert "hout-test-pol" not in ids_in_metrics
    assert "hout-test-sci" not in ids_in_metrics


def test_internal_manifest_includes_held_out(tmp_path: Path):
    store, ho_path = _seed_with_heldout(tmp_path)
    corpus = load_corpus(held_out_path=ho_path)
    mf = build_manifest(store=store, corpus=corpus, week_id="2026-W16",
                        history_weeks=0, bootstrap_seed=1, include_held_out=True)
    ids_in_prompts = {p["prompt_id"] for p in mf["prompts"]}
    assert "hout-test-pol" in ids_in_prompts
    ids_in_metrics = {m["prompt_id"] for m in mf["metrics"]}
    assert {"hout-test-pol", "hout-test-sci"}.issubset(ids_in_metrics)


# --------------- Site build defensive leak check ---------------

def test_site_build_refuses_manifest_with_held_out_prompt(tmp_path: Path):
    # Hand-craft a minimal manifest with one held-out prompt.
    bad_manifest = {
        "schema_version": 2,
        "snapshot": {
            "week_id": "2026-W16",
            "generated_at": "2026-04-19T00:00:00+00:00",
            "corpus_git_sha": "test",
            "pipeline_version": "0.1.0",
        },
        "models": [],
        "prompts": [
            {"prompt_id": "oops-held-out", "axis": "political", "title": "x",
             "text_hash": "0" * 64, "description": "leaked", "held_out": True},
        ],
        "metrics": [],
        "history": [],
        "flagged": [],
    }
    manifest_path = tmp_path / "bad-manifest.json"
    manifest_path.write_text(json.dumps(bad_manifest))
    result = subprocess.run(
        ["uv", "run", "python",
         str(REPO_ROOT / "site" / "src" / "build.py"),
         "--manifest", str(manifest_path),
         "--out", str(tmp_path / "dist")],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode != 0
    assert "held-out" in (result.stderr + result.stdout).lower()


# --------------- Comparison analyzer ---------------

def test_compare_holdout_detects_divergence():
    prompts = [
        Prompt(id="pub-1", axis="political", title="t", text="x", held_out=False),
        Prompt(id="pub-2", axis="political", title="t", text="x", held_out=False),
        Prompt(id="ho-1",  axis="political", title="t", text="x", held_out=True),
        Prompt(id="ho-2",  axis="political", title="t", text="x", held_out=True),
    ]
    metrics = [
        # Public: low refusal
        {"prompt_id": "pub-1", "refusal_rate": 0.10, "hedge_density": 2.0},
        {"prompt_id": "pub-2", "refusal_rate": 0.10, "hedge_density": 2.0},
        # Held-out: much higher refusal on same axis -> divergence
        {"prompt_id": "ho-1", "refusal_rate": 0.80, "hedge_density": 5.0},
        {"prompt_id": "ho-2", "refusal_rate": 0.80, "hedge_density": 5.0},
    ]
    report = compare_holdout(week_id="2026-W16", prompts=prompts, metrics=metrics)
    assert len(report.per_axis) == 1
    comp = report.per_axis[0]
    assert comp.axis == "political"
    assert comp.refusal_delta == pytest.approx(0.70, abs=1e-6)
    assert report.divergence_score > 0.5
    assert "divergence" in report.verdict()


def test_compare_holdout_no_signal_when_aligned():
    prompts = [
        Prompt(id="pub-1", axis="political", title="t", text="x", held_out=False),
        Prompt(id="ho-1",  axis="political", title="t", text="x", held_out=True),
    ]
    metrics = [
        {"prompt_id": "pub-1", "refusal_rate": 0.1, "hedge_density": 2.0},
        {"prompt_id": "ho-1",  "refusal_rate": 0.1, "hedge_density": 2.0},
    ]
    report = compare_holdout(week_id="2026-W16", prompts=prompts, metrics=metrics)
    assert report.divergence_score == pytest.approx(0.0)
    assert "no meaningful" in report.verdict()
