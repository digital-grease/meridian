"""inspect-week CLI command smoke tests.

Builds a minimal raw-storage tree + run log in a tmp dir, monkeypatches
the module-level ``REPO_ROOT`` used by the command, and runs the
command directly (not via subprocess, so the test stays fast).
"""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drift_audit.pipeline import cli as cli_module
from drift_audit.pipeline.run_log import append_run_log
from drift_audit.runners.base import Sample
from drift_audit.sampling.orchestrator import RunOutcome


def _fake_sample(prompt_id: str, model_id: str, idx: int) -> Sample:
    return Sample(
        prompt_id=prompt_id,
        model_id=model_id,
        provider="fake",
        request_index=idx,
        temperature=1.0,
        max_tokens=1024,
        text="substantive answer",
        model_version_string=f"{model_id}-2026-04-01",
        stop_reason="stop",
        latency_ms=1,
        captured_at=datetime(2026, 4, 19, tzinfo=timezone.utc),
    )


@pytest.fixture
def _patched_repo(tmp_path: Path, monkeypatch) -> Path:
    """Point cli.REPO_ROOT at a tmp dir with minimal scaffolding."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "raw").mkdir()
    monkeypatch.setattr(cli_module, "REPO_ROOT", tmp_path)
    # Every path we look up resolves under tmp_path via REPO_ROOT
    # interpolation in the command itself.
    return tmp_path


def test_inspect_week_empty_week(_patched_repo, capsys):
    ns = argparse.Namespace(
        config=None,
        week="2026-W99",  # no data seeded for this week
        json=False,
    )
    rc = cli_module._cmd_inspect_week(ns)
    assert rc == 0
    out = capsys.readouterr().out
    assert "2026-W99" in out
    assert "no raw samples found" in out
    assert "no entries for this week" in out


def test_inspect_week_with_partial_and_complete(_patched_repo, capsys, monkeypatch):
    # Seed two (prompt × model) pairs for one week. One is complete, one is partial.
    from drift_audit.storage import LocalSampleStore
    from drift_audit.config import load_config
    from drift_audit.corpus import load_corpus

    config = load_config(None)
    expected = config.sampling.n_default_temp + config.sampling.n_zero_temp
    corpus = load_corpus()
    first_two = corpus.public()[:2]
    complete_prompt = first_two[0].id
    partial_prompt = first_two[1].id

    raw_dir = _patched_repo / config.storage.raw_dir
    store = LocalSampleStore(raw_dir)
    for i in range(expected):
        store.append("2026-W16", "fake-model-1", complete_prompt,
                     _fake_sample(complete_prompt, "fake-model-1", i))
    # Seed only 1 sample for the "partial" pair.
    store.append("2026-W16", "fake-model-1", partial_prompt,
                 _fake_sample(partial_prompt, "fake-model-1", 0))

    ns = argparse.Namespace(config=None, week="2026-W16", json=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_module._cmd_inspect_week(ns)
    assert rc == 0
    report = json.loads(buf.getvalue())
    assert report["week_id"] == "2026-W16"
    assert report["expected_per_pair"] == expected
    assert len(report["per_model"]) == 1
    model_row = report["per_model"][0]
    assert model_row["model_id"] == "fake-model-1"
    statuses = {p["prompt_id"]: p["status"] for p in model_row["pairs"]}
    assert statuses[complete_prompt] == "complete"
    assert statuses[partial_prompt] == "partial"
    # Remaining prompts have no samples — "missing".
    other_missing = [p for p in model_row["pairs"]
                     if p["status"] == "missing"]
    assert len(other_missing) == len(corpus.public()) - 2


def _minimal_manifest(
    week_id: str,
    *,
    refusal_rate: float,
    significant_after_bh: bool,
) -> dict:
    return {
        "schema_version": 2,
        "snapshot": {
            "week_id": week_id,
            "generated_at": "2026-04-19T00:00:00+00:00",
            "corpus_git_sha": "abc1234",
            "pipeline_version": "0.1.0",
        },
        "models": [{
            "model_id": "fake-model-1",
            "display_name": "Fake",
            "provider": "fake",
            "version_string": "fake-2026-04-19",
            "available": True,
        }],
        "prompts": [{
            "prompt_id": "p1", "axis": "political", "title": "t",
            "text_hash": "0" * 64, "held_out": False,
        }],
        "metrics": [{
            "prompt_id": "p1",
            "model_id": "fake-model-1",
            "n_samples": 20,
            "refusal_rate": refusal_rate,
            "refusal_ci": {"lower": 0.0, "upper": 1.0},
            "hedge_density": 1.2,
            "length": {"median": 100.0, "p25": 80.0, "p75": 120.0, "n": 20},
            "stance": "neutral",
            "stance_confidence": 0.85,
            "embedding_centroid_shift": None,
            "refusal_drift": {
                "p_value": 0.01,
                "adjusted_p_value": 0.03,
                "significant_after_bh": significant_after_bh,
            },
            "hedge_drift": None,
            "length_drift": None,
            "change_points": {"refusal_rate": [], "hedge_density": [], "length_median": []},
            "sample_s3_uris": [],
            "flagged_for_review": False,
            "flag_reason": None,
        }],
        "history": [],
        "flagged": [],
    }


def test_dump_manifest_header(_patched_repo, tmp_path, capsys):
    manifest = _minimal_manifest("2026-W16", refusal_rate=0.20, significant_after_bh=True)
    m_path = tmp_path / "m.json"
    m_path.write_text(json.dumps(manifest))
    ns = argparse.Namespace(manifest=m_path, diff=None)
    rc = cli_module._cmd_dump_manifest(ns)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Schema version: 2" in out
    assert "2026-W16" in out
    assert "fake-model-1" in out


def test_dump_manifest_diff_shows_delta_and_bh_flip(_patched_repo, tmp_path, capsys):
    prior = _minimal_manifest("2026-W15", refusal_rate=0.10, significant_after_bh=False)
    current = _minimal_manifest("2026-W16", refusal_rate=0.90, significant_after_bh=True)
    p_path = tmp_path / "prior.json"
    c_path = tmp_path / "current.json"
    p_path.write_text(json.dumps(prior))
    c_path.write_text(json.dumps(current))
    ns = argparse.Namespace(manifest=c_path, diff=p_path)
    rc = cli_module._cmd_dump_manifest(ns)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Diff against" in out
    assert "p1 × fake-model-1" in out
    assert "↑" in out
    assert "+0.800" in out
    assert "significance: False → True" in out


def test_inspect_week_surfaces_run_log(_patched_repo, capsys):
    from drift_audit.config import load_config

    config = load_config(None)
    # Write one run-log entry for the target week.
    outcome = RunOutcome(
        week_id="2026-W16",
        total_samples_written=42,
        pairs_complete=10,
        pairs_skipped=1,
        pairs_failed=2,
        per_runner_samples={"anthropic/claude-opus-4-7": 42},
        errors=[],
    )
    now = datetime(2026, 4, 19, 1, 15, tzinfo=timezone.utc)
    append_run_log(
        _patched_repo / "data" / "run_log.jsonl",
        started_at=now,
        finished_at=now,
        week_id="2026-W16",
        config=config,
        outcome=outcome,
        estimated_cost_usd=2.15,
        actual_cost_usd=2.01,
    )
    ns = argparse.Namespace(config=None, week="2026-W16", json=False)
    rc = cli_module._cmd_inspect_week(ns)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Run log" in out
    assert "$2.15" in out
    assert "$2.01" in out
