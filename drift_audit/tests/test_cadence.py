"""Cadence filter tests.

The production meaning: a runner with ``cadence: even_weeks`` belongs in
the run for weeks whose ISO week number is even, and so on. Alternation
between two runners (one on even, one on odd) is the canonical use case.
"""
from __future__ import annotations

import pytest

from drift_audit.config import (
    PipelineConfig,
    RunnerSpec,
    SamplingSpec,
    build_runners,
    should_run_in_week,
)


def test_every_week_always_runs():
    for wk in ("2026-W01", "2026-W02", "2026-W52"):
        assert should_run_in_week("every_week", wk) is True


def test_even_weeks():
    assert should_run_in_week("even_weeks", "2026-W02") is True
    assert should_run_in_week("even_weeks", "2026-W16") is True
    assert should_run_in_week("even_weeks", "2026-W17") is False
    assert should_run_in_week("even_weeks", "2026-W01") is False


def test_odd_weeks():
    assert should_run_in_week("odd_weeks", "2026-W15") is True
    assert should_run_in_week("odd_weeks", "2026-W17") is True
    assert should_run_in_week("odd_weeks", "2026-W16") is False


def test_alternation_covers_every_week():
    for wk_num in range(1, 53):
        wk = f"2026-W{wk_num:02d}"
        ran = should_run_in_week("even_weeks", wk) or should_run_in_week("odd_weeks", wk)
        assert ran, f"week {wk} covered by neither even nor odd"


def test_unparseable_week_id_raises():
    with pytest.raises(ValueError, match="unparseable"):
        should_run_in_week("even_weeks", "not-a-week")


def test_build_runners_respects_cadence_ollama_only():
    """Cadence filtering with Ollama-only runners (no SDK construction)."""
    config = PipelineConfig(
        sampling=SamplingSpec(),
        runners=[
            RunnerSpec(provider="ollama", model_id="every",
                       enabled=True, cadence="every_week"),
            RunnerSpec(provider="ollama", model_id="even",
                       enabled=True, cadence="even_weeks"),
            RunnerSpec(provider="ollama", model_id="odd",
                       enabled=True, cadence="odd_weeks"),
        ],
    )
    even = {r.model_id for r in build_runners(config, week_id="2026-W16")}
    assert even == {"every", "even"}

    odd = {r.model_id for r in build_runners(config, week_id="2026-W17")}
    assert odd == {"every", "odd"}

    # No week_id: cadence filter disabled, all enabled runners returned.
    assert len(build_runners(config, week_id=None)) == 3


def test_build_runners_disabled_still_excluded():
    config = PipelineConfig(
        sampling=SamplingSpec(),
        runners=[
            RunnerSpec(provider="ollama", model_id="llama3.2:3b",
                       enabled=False, cadence="every_week"),
        ],
    )
    assert build_runners(config, week_id="2026-W16") == []
    assert build_runners(config, week_id=None) == []
