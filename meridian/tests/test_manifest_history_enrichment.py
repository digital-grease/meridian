"""Tests for _enrich_models_with_history.

Carries forward previously-seen models from prior committed manifests
so model index pages stay stable across cadence-alternated weeks
(Opus on even weeks, GPT-5.1 on odd, etc.). Without this, half the
model pages disappear every week and the link-rot guard fails.
"""
from __future__ import annotations

import json
from pathlib import Path

from meridian.pipeline.manifest_writer import _enrich_models_with_history


def _write_manifest(path: Path, models: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"models": models}))


def test_no_prior_manifests_dir_returns_input_unchanged(tmp_path: Path):
    current = [{"model_id": "x", "available": True}]
    out = _enrich_models_with_history(current, tmp_path / "does-not-exist")
    assert out == current


def test_empty_manifests_dir_returns_input_unchanged(tmp_path: Path):
    current = [{"model_id": "x", "available": True}]
    (tmp_path / "manifests").mkdir()
    out = _enrich_models_with_history(current, tmp_path / "manifests")
    assert out == current


def test_carries_forward_unseen_model_with_available_false(tmp_path: Path):
    manifests = tmp_path / "manifests"
    _write_manifest(manifests / "2026-W17.json", [
        {"model_id": "gpt-5.1", "display_name": "GPT-5.1",
         "provider": "openai", "version_string": "gpt-5.1-2026-04",
         "available": True},
    ])
    current = [{"model_id": "claude-opus-4-7", "display_name": "Opus 4.7",
                "provider": "anthropic", "version_string": "opus-4.7",
                "available": True}]

    out = _enrich_models_with_history(current, manifests)

    assert {m["model_id"] for m in out} == {"claude-opus-4-7", "gpt-5.1"}
    by_id = {m["model_id"]: m for m in out}
    # current week's entry stays as-is.
    assert by_id["claude-opus-4-7"]["available"] is True
    # historical entry is marked unavailable so templates can show it
    # didn't run this week.
    assert by_id["gpt-5.1"]["available"] is False
    # other identity fields preserved from the prior manifest.
    assert by_id["gpt-5.1"]["display_name"] == "GPT-5.1"
    assert by_id["gpt-5.1"]["provider"] == "openai"


def test_does_not_clobber_current_week_entry(tmp_path: Path):
    """A model present in both current and history keeps its current
    metadata + available=True, regardless of what the prior manifest said."""
    manifests = tmp_path / "manifests"
    _write_manifest(manifests / "2026-W15.json", [
        {"model_id": "gpt-5.1", "display_name": "Old name",
         "provider": "openai", "version_string": "old",
         "available": False},
    ])
    current = [{"model_id": "gpt-5.1", "display_name": "GPT-5.1",
                "provider": "openai", "version_string": "new",
                "available": True}]

    out = _enrich_models_with_history(current, manifests)

    assert len(out) == 1
    assert out[0]["display_name"] == "GPT-5.1"
    assert out[0]["available"] is True
    assert out[0]["version_string"] == "new"


def test_walks_multiple_prior_weeks_in_descending_order(tmp_path: Path):
    """When two prior manifests both describe the same historical
    model, the newer one wins (sorted descending → first hit kept)."""
    manifests = tmp_path / "manifests"
    _write_manifest(manifests / "2026-W15.json", [
        {"model_id": "gpt-5.1", "display_name": "Older",
         "provider": "openai", "version_string": "v1", "available": True},
    ])
    _write_manifest(manifests / "2026-W17.json", [
        {"model_id": "gpt-5.1", "display_name": "Newer",
         "provider": "openai", "version_string": "v2", "available": True},
    ])
    out = _enrich_models_with_history([], manifests)
    assert len(out) == 1
    assert out[0]["display_name"] == "Newer"


def test_corrupt_manifest_skipped_silently(tmp_path: Path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "2026-W17.json").write_text("{not valid json")
    _write_manifest(manifests / "2026-W18.json", [
        {"model_id": "x", "display_name": "X",
         "provider": "p", "version_string": "v", "available": True},
    ])
    out = _enrich_models_with_history([], manifests)
    assert {m["model_id"] for m in out} == {"x"}
