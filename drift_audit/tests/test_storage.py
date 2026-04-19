"""LocalSampleStore round-trip tests."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from drift_audit.runners.base import Sample
from drift_audit.storage import LocalSampleStore


def _mk_sample(idx: int, text: str = "hello world") -> Sample:
    return Sample(
        prompt_id="pol-abortion-legal",
        model_id="claude-opus-4-7",
        provider="anthropic",
        request_index=idx,
        temperature=1.0,
        max_tokens=1024,
        text=text,
        model_version_string="claude-opus-4-7-20260318",
        stop_reason="end_turn",
        finish_reason=None,
        input_tokens=10,
        output_tokens=40,
        request_id=f"req_{idx}",
        api_version="anthropic-sdk-0.40.0",
        latency_ms=1200,
        captured_at=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
        safety_flags=[],
    )


def test_append_read_roundtrip(tmp_path: Path):
    store = LocalSampleStore(tmp_path)
    for i in range(5):
        store.append("2026-W16", "claude-opus-4-7", "pol-abortion-legal",
                     _mk_sample(i, text=f"response {i}"))

    back = store.read("2026-W16", "claude-opus-4-7", "pol-abortion-legal")
    assert len(back) == 5
    assert [s.request_index for s in back] == [0, 1, 2, 3, 4]
    assert back[2].text == "response 2"


def test_count_matches_append_count(tmp_path: Path):
    store = LocalSampleStore(tmp_path)
    assert store.count("2026-W16", "m", "p") == 0
    store.append("2026-W16", "m", "p", _mk_sample(0))
    store.append("2026-W16", "m", "p", _mk_sample(1))
    assert store.count("2026-W16", "m", "p") == 2


def test_weeks_and_models_discovery(tmp_path: Path):
    store = LocalSampleStore(tmp_path)
    store.append("2026-W15", "m1", "p1", _mk_sample(0))
    store.append("2026-W16", "m1", "p1", _mk_sample(0))
    store.append("2026-W16", "m2", "p1", _mk_sample(0))

    assert store.weeks() == ["2026-W15", "2026-W16"]
    assert store.models_for_week("2026-W16") == ["m1", "m2"]
    assert store.prompts_for("2026-W16", "m1") == ["p1"]


def test_read_missing_returns_empty(tmp_path: Path):
    store = LocalSampleStore(tmp_path)
    assert store.read("nope", "nope", "nope") == []
