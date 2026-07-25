"""End-to-end: running the CLI ``run`` command appends to data/run_log.jsonl.

The run log has been testable in isolation since Phase 2 shipped, but
until now no production code path called ``append_run_log``. This test
is the regression gate on that integration — if someone removes the
append in ``_cmd_run``, ``inspect-week`` goes blind.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from meridian.pipeline import cli as cli_module
from meridian.pipeline.run_log import read_run_log
from meridian.runners.base import Runner, Sample


class _FakeRunner(Runner):
    """Canned substantive answers. Carries token counts so the cost
    tracker has something to price (still $0 because 'fake' isn't in
    the pricing table, which is the correct behavior)."""
    provider = "fake"

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    async def sample(self, prompt, *, prompt_id, request_index, temperature, max_tokens=1024):
        return Sample(
            prompt_id=prompt_id,
            model_id=self.model_id,
            provider=self.provider,
            request_index=request_index,
            temperature=temperature,
            max_tokens=max_tokens,
            text="This is a substantive answer.",
            model_version_string=f"{self.model_id}-2026-04-19",
            stop_reason="stop",
            latency_ms=1,
            captured_at=datetime.now(timezone.utc),
            input_tokens=100,
            output_tokens=200,
        )


@pytest.fixture
def _isolated_repo(tmp_path: Path, monkeypatch) -> Path:
    """REPO_ROOT-pinned paths point into a tmpdir so the real
    ``data/run_log.jsonl`` and ``site/fixtures/`` are untouched."""
    (tmp_path / "data").mkdir()
    (tmp_path / "site" / "fixtures").mkdir(parents=True)
    monkeypatch.setattr(cli_module, "REPO_ROOT", tmp_path)
    return tmp_path


def _trimmed_config(path: Path) -> Path:
    """Minimal config: one fake runner, small sample counts for speed."""
    cfg = {
        "sampling": {
            "n_default_temp": 2,
            "n_zero_temp": 1,
            "max_tokens": 64,
            "concurrency_per_provider": 2,
        },
        "storage": {"raw_dir": "data/raw"},
        "runners": [
            {"provider": "anthropic", "model_id": "claude-opus-4-7",
             "enabled": True, "cadence": "every_week"},
        ],
    }
    config_path = path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg))
    return config_path


def test_cmd_run_appends_run_log_entry(
    _isolated_repo: Path, monkeypatch, capsys,
):
    """Exercise _cmd_run end-to-end with a FakeRunner and verify a row
    lands in data/run_log.jsonl."""
    config_path = _trimmed_config(_isolated_repo)

    # Swap real provider runners for the FakeRunner. _build_context
    # calls meridian.config.build_runners; intercept that.
    from meridian import config as config_module

    def _fake_build_runners(cfg, *, week_id=None):
        return [_FakeRunner("claude-opus-4-7")]

    monkeypatch.setattr(config_module, "build_runners", _fake_build_runners)
    # _build_context imports build_runners from meridian.config at
    # module scope via `from meridian.config import ... build_runners`,
    # so also patch the already-bound name on the CLI module.
    monkeypatch.setattr(cli_module, "build_runners", _fake_build_runners)

    # Use a tiny corpus subset by trimming public() via monkeypatching
    # the module used inside _cmd_run. Cleaner: trim the corpus object.
    from meridian.corpus import load_corpus

    full = load_corpus()

    class _TrimmedCorpus:
        def __init__(self, inner):
            self._inner = inner
            self._subset = inner.public()[:2]

        def public(self):
            return self._subset

        def all(self):
            return self._subset

        def by_id(self, pid):
            return self._inner.by_id(pid)

        def by_axis(self, axis):
            return [p for p in self._subset if p.axis == axis]

        @property
        def has_held_out(self):
            return False

        def __getattr__(self, name):
            # Delegate anything not overridden above (corpus_version,
            # schema_version, ...) to the real corpus. Without this the
            # shim silently diverges from Corpus every time a field is
            # added, and the test fails for a reason unrelated to what
            # it covers.
            return getattr(self._inner, name)

    monkeypatch.setattr(cli_module, "load_corpus", lambda: _TrimmedCorpus(full))

    ns = argparse.Namespace(
        config=config_path,
        week="2026-W16",
        force=False,
        yes=True,
        dry_run=False,
    )
    import asyncio
    rc = asyncio.run(cli_module._cmd_run(ns))
    assert rc == 0, f"unexpected non-zero exit: stderr={capsys.readouterr().err}"

    log_path = _isolated_repo / "data" / "run_log.jsonl"
    assert log_path.exists(), "run log was not created"
    entries = read_run_log(log_path)
    assert len(entries) == 1, f"expected exactly one entry, got {len(entries)}"
    entry = entries[0]
    assert entry.week_id == "2026-W16"
    assert entry.pairs_complete == 2
    assert entry.pairs_failed == 0
    assert entry.total_samples_written == 2 * 3  # 2 prompts × (n_default+n_zero)
    # Fake provider isn't in PRICING → actual cost is $0, but the entry
    # still records whatever the estimator predicted.
    assert entry.actual_cost_usd == 0.0
    # per_runner_samples keys are "provider/model_id"
    assert any("claude-opus-4-7" in k for k in entry.per_runner_samples)


def test_cmd_run_dry_run_does_not_append(
    _isolated_repo: Path, monkeypatch,
):
    """--dry-run exits before sampling; no log entry should appear."""
    config_path = _trimmed_config(_isolated_repo)

    from meridian import config as config_module

    def _fake_build_runners(cfg, *, week_id=None):
        return [_FakeRunner("claude-opus-4-7")]

    monkeypatch.setattr(config_module, "build_runners", _fake_build_runners)
    monkeypatch.setattr(cli_module, "build_runners", _fake_build_runners)

    ns = argparse.Namespace(
        config=config_path,
        week="2026-W16",
        force=False,
        yes=True,
        dry_run=True,
    )
    import asyncio
    rc = asyncio.run(cli_module._cmd_run(ns))
    assert rc == 0
    assert not (_isolated_repo / "data" / "run_log.jsonl").exists()
