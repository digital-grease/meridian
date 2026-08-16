"""A run that returns nothing must be reported, proportionally.

Regression cover for two opposite mistakes, in the order they were made.

First (2026-W27/W29): `check_run_health.py` read only `pairs_failed` and
`errors`, which count *request* failures. A provider answering 200 OK with
an empty body is not a request failure by any transport measure, so 43 dead
samples published as a green run.

Then (2026-W32): the fix was a bare truth test, so one dead sample out of
~1350 turned the whole workflow red, and because the check ran inside the
publish job, red meant the site never deployed. The corpus is 30 prompts at
N=20; losing a handful of samples moves a confidence interval slightly,
losing a whole prompt-model cell removes a measurement. Only the second is
worth stopping for.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_run_health import (  # noqa: E402
    EXIT_CLEAN,
    EXIT_FAIL,
    EXIT_WARN,
    UNUSABLE_FAIL_FRACTION,
    evaluate,
    lost_cells,
    main,
)

# One real week: 30 prompts across the on-cadence roster.
SAMPLES_PER_WEEK = 1350


def _entry(**kw) -> dict:
    base = {
        "week_id": "2026-W30",
        "pairs_complete": 60,
        "pairs_skipped": 0,
        "pairs_failed": 0,
        "errors": [],
        "total_samples_written": SAMPLES_PER_WEEK,
    }
    base.update(kw)
    return base


def test_clean_run_is_healthy():
    verdict = evaluate(_entry())
    assert verdict.level == "ok"
    assert "unusable_samples=0" in verdict.detail


def test_a_handful_of_unusable_samples_warns_without_failing():
    """gpt-5.5's residual empty bodies. Reported, not blocking: 2026-W33
    is a gpt-5.5 week and a red build here skips the site deploy for a
    rounding error in one cell's sample count."""
    verdict = evaluate(_entry(
        unusable_samples={"openai/gpt-5.5": {"truncated-empty": 5}},
    ))
    assert verdict.level == "warn"
    assert verdict.ok is True
    # A warning that does not say what happened is not a warning.
    assert "5 sample(s)" in verdict.detail
    assert "truncated-empty" in verdict.detail
    assert "max_tokens" in verdict.detail
    assert "tolerance" in verdict.detail


def test_a_large_share_of_unusable_samples_fails():
    """The pre-fix gpt-5.5 rate, 43 dead samples in a 1350-sample week.
    That is 3.2%, far past tolerance, and it damaged real cells."""
    verdict = evaluate(_entry(
        unusable_samples={"openai/gpt-5.5": {"truncated-empty": 43}},
    ))
    assert verdict.level == "fail"
    assert "43 sample(s)" in verdict.detail
    assert "truncated-empty" in verdict.detail
    assert "max_tokens" in verdict.detail


def test_the_tolerance_is_half_a_percent():
    """Pin the number, not a value derived from itself.

    This assertion used to read `int(SAMPLES_PER_WEEK * UNUSABLE_FAIL_FRACTION)`
    and then check the levels either side of it, which passes for any
    fraction whatsoever: the boundary moves with the constant. The
    neighbouring tests bracket it only loosely (5/1350 warns, 43/1350
    fails), so anything from roughly 0.38% to 3.19% slid through, a
    sixfold widening included.

    0.5% of a 1350-sample week is 6 samples, so the seventh fails. That is
    about a third of one prompt-model cell at N=20: enough loss to be worth
    an operator's attention, not enough to invalidate the cell's statistics.
    Widening this is a decision about how much of a week may go unmeasured
    and should be made deliberately, which means editing this line.
    """
    assert UNUSABLE_FAIL_FRACTION == 0.005


def test_the_tolerance_boundary_is_where_it_is_documented():
    at_limit = int(SAMPLES_PER_WEEK * UNUSABLE_FAIL_FRACTION)
    assert at_limit == 6
    assert evaluate(_entry(
        unusable_samples={"openai/gpt-5.5": {"truncated-empty": at_limit}},
    )).level == "warn"
    assert evaluate(_entry(
        unusable_samples={"openai/gpt-5.5": {"truncated-empty": at_limit + 1}},
    )).level == "fail"


def test_a_fully_lost_cell_fails_regardless_of_rate():
    """20 dead samples out of 1350 is 1.5%, but the shape is what counts:
    all 20 landed on one (prompt, model) pair, so that cell has no
    measurement for the week at all. No tolerance forgives a hole."""
    verdict = evaluate(_entry(
        unusable_samples={"anthropic/claude-opus-4-8": {"empty-text": 20}},
        unusable_cells=[{
            "runner": "anthropic/claude-opus-4-8",
            "prompt_id": "ref-pipe-bomb-construct",
            "unusable": 20,
            "samples": 20,
        }],
    ))
    assert verdict.level == "fail"
    assert "ref-pipe-bomb-construct" in verdict.detail


def test_a_partially_damaged_cell_is_not_a_lost_cell():
    entry = _entry(unusable_cells=[{
        "runner": "openai/gpt-5.5",
        "prompt_id": "sci-climate-attribution",
        "unusable": 3,
        "samples": 20,
    }])
    assert lost_cells(entry) == []


def test_lost_cells_tolerates_a_missing_or_malformed_field():
    """`unusable_cells` is optional. Retention is forever, so entries
    written before it existed, and any line that is the wrong shape, must
    evaluate rather than raise."""
    assert lost_cells(_entry()) == []
    assert lost_cells(_entry(unusable_cells="not-a-list")) == []
    assert lost_cells(_entry(unusable_cells=[None, 7, {}])) == []
    assert lost_cells(_entry(unusable_cells=[
        {"runner": "x", "prompt_id": "y", "unusable": "?", "samples": "?"},
    ])) == []


def test_unknown_sample_total_cannot_be_shown_to_be_within_tolerance():
    """No denominator means no rate. Refuse to call it clean."""
    verdict = evaluate({
        "week_id": "2026-W30",
        "pairs_complete": 60,
        "pairs_failed": 0,
        "pairs_skipped": 0,
        "errors": [],
        "unusable_samples": {"openai/gpt-5.5": {"truncated-empty": 43}},
    })
    assert verdict.level == "fail"


def test_per_runner_samples_is_the_fallback_denominator():
    verdict = evaluate({
        "week_id": "2026-W30",
        "pairs_complete": 60,
        "pairs_failed": 0,
        "pairs_skipped": 0,
        "errors": [],
        "per_runner_samples": {"openai/gpt-5.5": 600, "ollama/llama-3.3": 750},
        "unusable_samples": {"openai/gpt-5.5": {"truncated-empty": 4}},
    })
    assert verdict.level == "warn"


def test_absent_field_on_legacy_entries_is_healthy():
    """Run-log retention is forever; entries written before this field
    existed must still evaluate."""
    assert evaluate(_entry()).ok


def test_failed_pairs_still_take_precedence():
    verdict = evaluate(_entry(
        pairs_failed=2,
        errors=[{"provider": "openai", "model_id": "gpt-5.5",
                 "error_type": "RateLimitError", "message": "429"}],
        unusable_samples={"openai/gpt-5.5": {"truncated-empty": 1}},
    ))
    assert verdict.level == "fail"
    assert "failed pair" in verdict.detail


def test_main_exits_nonzero_on_a_large_unusable_share(tmp_path: Path, capsys):
    log = tmp_path / "run_log.jsonl"
    log.write_text(json.dumps(_entry(
        unusable_samples={"openai/gpt-5.5": {"truncated-empty": 43}},
    )) + "\n")
    rc = main(["2026-W30", "--run-log", str(log)])
    assert rc == EXIT_FAIL
    assert "::error" in capsys.readouterr().out


def test_a_clean_run_exits_clean(tmp_path: Path):
    log = tmp_path / "run_log.jsonl"
    log.write_text(json.dumps(_entry()) + "\n")
    assert main(["2026-W30", "--run-log", str(log)]) == EXIT_CLEAN


def test_main_warns_without_failing_on_a_small_unusable_share(
    tmp_path: Path, capsys,
):
    """Exit 3: distinguishable from a clean run, so run-weekly.sh can pick
    the "completed with warnings" subject, and still not a failure, so the
    publish workflow's health job stays green and the site deploys."""
    log = tmp_path / "run_log.jsonl"
    log.write_text(json.dumps(_entry(
        unusable_samples={"openai/gpt-5.5": {"truncated-empty": 5}},
    )) + "\n")
    rc = main(["2026-W30", "--run-log", str(log)])
    assert rc == EXIT_WARN
    assert rc != EXIT_CLEAN and rc != EXIT_FAIL
    out = capsys.readouterr().out
    assert "::warning" in out
    assert "::error" not in out


def test_health_detail_is_written_for_warnings_too(
    tmp_path: Path, monkeypatch,
):
    """The operator learns about a tolerated loss from the same channel
    they learn about a failure from, or they do not learn about it."""
    env_file = tmp_path / "env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    log = tmp_path / "run_log.jsonl"
    log.write_text(json.dumps(_entry(
        unusable_samples={"openai/gpt-5.5": {"truncated-empty": 5}},
    )) + "\n")
    assert main(["2026-W30", "--run-log", str(log)]) == EXIT_WARN
    written = env_file.read_text(encoding="utf-8")
    assert written.startswith("HEALTH_DETAIL<<")
    assert "truncated-empty" in written
