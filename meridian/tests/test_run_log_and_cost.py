"""Run log persistence + actual-cost computation."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from meridian.config import PipelineConfig, RunnerSpec, SamplingSpec
from meridian.pipeline.run_log import append_run_log, read_run_log
from meridian.runners.base import Sample
from meridian.sampling.cost import compute_actual_cost
from meridian.sampling.orchestrator import PairError, RunOutcome


def _cfg():
    return PipelineConfig(
        sampling=SamplingSpec(),
        runners=[
            RunnerSpec(provider="ollama", model_id="llama3.2:3b", enabled=True),
            RunnerSpec(provider="anthropic", model_id="claude-haiku-4-5-20251001", enabled=False),
        ],
    )


def _outcome():
    return RunOutcome(
        week_id="2026-W16",
        total_samples_written=42,
        pairs_complete=3,
        pairs_skipped=1,
        pairs_failed=1,
        per_runner_samples={"ollama/llama3.2:3b": 42},
        errors=[
            PairError(provider="anthropic", model_id="claude",
                      prompt_id="pol-x", error_type="RateLimitError", message="quota"),
        ],
    )


def test_run_log_roundtrip(tmp_path: Path):
    log = tmp_path / "run_log.jsonl"
    started = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
    finished = datetime(2026, 4, 19, 0, 15, tzinfo=timezone.utc)
    entry = append_run_log(
        log,
        started_at=started, finished_at=finished,
        week_id="2026-W16",
        config=_cfg(),
        outcome=_outcome(),
        estimated_cost_usd=3.00,
        actual_cost_usd=2.87,
        note="first real run",
    )
    assert entry.week_id == "2026-W16"
    assert entry.pairs_failed == 1

    entries = read_run_log(log)
    assert len(entries) == 1
    got = entries[0]
    assert got.note == "first real run"
    assert got.actual_cost_usd == 2.87
    assert got.per_runner_samples == {"ollama/llama3.2:3b": 42}
    assert len(got.errors) == 1


def test_run_log_append_preserves_prior(tmp_path: Path):
    log = tmp_path / "run_log.jsonl"
    for wk in ("2026-W15", "2026-W16"):
        append_run_log(
            log,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            week_id=wk,
            config=_cfg(),
            outcome=_outcome(),
            estimated_cost_usd=1.0,
            actual_cost_usd=1.0,
        )
    entries = read_run_log(log)
    assert [e.week_id for e in entries] == ["2026-W15", "2026-W16"]


# --- cost ---


def _s(provider: str, model_id: str, in_tokens: int | None, out_tokens: int | None) -> Sample:
    return Sample(
        prompt_id="p", model_id=model_id, provider=provider,
        request_index=0, temperature=1.0, max_tokens=1024,
        text="ok", model_version_string="v",
        input_tokens=in_tokens, output_tokens=out_tokens,
        latency_ms=1, captured_at=datetime.now(timezone.utc),
    )


def test_cost_sum_matches_pricing():
    samples = [
        _s("anthropic", "claude-haiku-4-5-20251001", 1_000_000, 1_000_000),
        _s("openai",    "gpt-4.1-mini",            1_000_000, 1_000_000),
        _s("ollama",    "llama3.2:3b",             1_000_000, 1_000_000),
    ]
    report = compute_actual_cost(samples)
    # Claude Haiku: 0.80 + 4.00 = 4.80
    # GPT-4.1-mini: 0.15 + 0.60 = 0.75
    # Ollama: 0
    assert report.total_usd == 5.55
    assert report.samples_priced == 3
    assert report.samples_skipped_no_tokens == 0
    assert report.by_runner["anthropic/claude-haiku-4-5-20251001"] == 4.80
    assert report.by_runner["openai/gpt-4.1-mini"] == 0.75


def test_cost_skips_samples_missing_tokens():
    samples = [
        _s("anthropic", "claude-haiku-4-5-20251001", None, 100),
        _s("anthropic", "claude-haiku-4-5-20251001", 100, None),
        _s("anthropic", "claude-haiku-4-5-20251001", 100, 100),
    ]
    report = compute_actual_cost(samples)
    assert report.samples_skipped_no_tokens == 2
    assert report.samples_priced == 1
    assert report.total_usd > 0.0


def test_cost_unknown_model_treated_as_free():
    samples = [_s("openai", "unknown-model-xyz", 100_000, 100_000)]
    report = compute_actual_cost(samples)
    assert report.total_usd == 0.0
    assert report.samples_priced == 1
