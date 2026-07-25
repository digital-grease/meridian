"""A run that returns nothing must not report as healthy.

Regression cover for the 2026-W27/W29 gap: `check_run_health.py` read
only `pairs_failed` and `errors`, which count *request* failures. A
provider answering 200 OK with an empty body is not a request failure by
any transport measure, so 43 dead samples published as a green run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_run_health import evaluate, main  # noqa: E402


def _entry(**kw) -> dict:
    base = {
        "week_id": "2026-W30",
        "pairs_complete": 60,
        "pairs_skipped": 0,
        "pairs_failed": 0,
        "errors": [],
    }
    base.update(kw)
    return base


def test_clean_run_is_healthy():
    healthy, detail = evaluate(_entry())
    assert healthy
    assert "unusable_samples=0" in detail


def test_unusable_samples_make_the_run_unhealthy():
    healthy, detail = evaluate(_entry(
        unusable_samples={"openai/gpt-5.5": {"truncated-empty": 43}},
    ))
    assert not healthy
    assert "43 sample(s)" in detail
    assert "truncated-empty" in detail
    # The message has to name the fix, not just the symptom.
    assert "max_tokens" in detail


def test_absent_field_on_legacy_entries_is_healthy():
    """Run-log retention is forever; entries written before this field
    existed must still evaluate."""
    healthy, _ = evaluate(_entry())
    assert healthy


def test_failed_pairs_still_take_precedence():
    healthy, detail = evaluate(_entry(
        pairs_failed=2,
        errors=[{"provider": "openai", "model_id": "gpt-5.5",
                 "error_type": "RateLimitError", "message": "429"}],
        unusable_samples={"openai/gpt-5.5": {"truncated-empty": 1}},
    ))
    assert not healthy
    assert "failed pair" in detail


def test_main_exits_nonzero_on_unusable(tmp_path: Path, capsys):
    log = tmp_path / "run_log.jsonl"
    log.write_text(json.dumps(_entry(
        unusable_samples={"openai/gpt-5.5": {"truncated-empty": 43}},
    )) + "\n")
    rc = main(["2026-W30", "--run-log", str(log)])
    assert rc == 1
    assert "::error" in capsys.readouterr().out
