"""Replay real production weeks through scripts/check_run_health.py.

Every other health test builds its entries by hand, which is how the
check came to pass weeks it should have failed: nobody wrote a fixture
shaped like a provider that wrote nothing. These run the published
run_log rows (copied byte for byte from ``data/run_log.jsonl`` on main
as of 2026-W39) and trimmed copies of the published manifests through
``main`` and pin the verdict each week should have produced:

* 2026-W19: clean, over two entries. The second, a resume on the EC2
  box, skipped all 60 pairs because they were stored and wrote 0. Must
  not page gpt-5.1 as holding nothing.
* 2026-W32: clean data that failed on 20 "empty" samples, which were
  refusals delivered through ``stop_reason``. Must not fail.
* 2026-W35: usable, and red only because 2026-W34 never ran. Must not
  fail once the gap ledger acknowledges 2026-W34.
* 2026-W36: Anthropic's balance ran out 73s in, 45 of 1200 samples.
  Must fail, titled as a billing failure for Anthropic.
* 2026-W37: green at the time, with stance dead on every cell.
* 2026-W38: Anthropic 0 of 1200. Must fail.
* 2026-W39: clean. Must be clean, not a warning about the same
  ref-wifi-unauthorized rejections every odd week.

Nothing here reads or writes under ``data/``: the fixtures live in
``meridian/tests/fixtures`` and the gap ledger is written to tmp_path.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_run_health.py"
_spec = importlib.util.spec_from_file_location("check_run_health_replay", _SCRIPT)
crh = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(crh)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RUN_LOG = FIXTURES / "run_log_production_2026-W39.jsonl"


def _manifest(week: str) -> Path:
    return FIXTURES / f"manifest_trimmed_{week}.json"


def _ledger(tmp_path: Path, *weeks: str) -> Path:
    path = tmp_path / "gaps.jsonl"
    path.write_text(
        "".join(
            json.dumps({"week_id": w, "reason": f"test: {w} lost"}) + "\n"
            for w in weeks
        ),
        encoding="utf-8",
    )
    return path


def _run(tmp_path: Path, monkeypatch, week: str, *, ledger: Path | None = None):
    """Run main() for one week; return (exit code, outputs dict)."""
    out = tmp_path / f"out-{week}"
    out.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("GITHUB_ENV", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    title_file = tmp_path / f"title-{week}"
    argv = [
        week, "--run-log", str(RUN_LOG),
        "--manifest", str(_manifest(week)),
        "--gaps", str(ledger or tmp_path / "no-ledger.jsonl"),
        "--title-file", str(title_file),
    ]
    rc = crh.main(argv)
    outputs = _parse_outputs(out.read_text(encoding="utf-8"))
    assert title_file.read_text(encoding="utf-8").strip() == outputs["health_title"]
    return rc, outputs


def _parse_outputs(text: str) -> dict[str, str]:
    """Read the heredoc form of a GitHub Actions file-command file."""
    outputs: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        key, delim = lines[i].split("<<", 1)
        body = []
        i += 1
        while lines[i] != delim:
            body.append(lines[i])
            i += 1
        outputs[key] = "\n".join(body)
        i += 1
    return outputs


LOST_WEEKS = ("2026-W30", "2026-W31", "2026-W34")


def test_the_fixture_is_the_published_log():
    """The replay is only worth something if the rows are the real ones."""
    weeks = [json.loads(line)["week_id"] for line in RUN_LOG.read_text().splitlines()]
    assert weeks[-1] == "2026-W39"
    assert {"2026-W32", "2026-W35", "2026-W36", "2026-W38"} <= set(weeks)
    assert "2026-W34" not in weeks


def test_w19_resume_that_skipped_every_pair_is_not_lost_data(tmp_path, monkeypatch):
    rc, out = _run(tmp_path, monkeypatch, "2026-W19")
    assert rc == crh.EXIT_CLEAN, out["health_title"]
    assert out["health_title"].startswith("OK 2026-W19")
    assert "NO-DATA" not in out["health_report"]


def test_w32_refusals_are_measurements_not_a_failure(tmp_path, monkeypatch):
    rc, out = _run(
        tmp_path, monkeypatch, "2026-W32", ledger=_ledger(tmp_path, *LOST_WEEKS)
    )
    # A warning about 2026-W31, which is genuinely the week before, and
    # nothing about the samples.
    assert rc == crh.EXIT_WARN
    assert out["health_title"] == "WARN 2026-W32 cadence GAP: 2026-W31 missing"
    # The 20 run_log "empty" samples are scored as refusals in the
    # published manifest, and the verdict says which number it used.
    assert "holds 0" in out["health_detail"]
    assert out["health_data_loss"] == "false"


def test_w35_acknowledged_gap_warns_instead_of_failing(tmp_path, monkeypatch):
    rc, out = _run(tmp_path, monkeypatch, "2026-W35")
    assert rc == crh.EXIT_FAIL  # without the ledger, W34 is news
    rc, out = _run(
        tmp_path, monkeypatch, "2026-W35", ledger=_ledger(tmp_path, *LOST_WEEKS)
    )
    assert rc == crh.EXIT_WARN
    assert out["health_title"] == "WARN 2026-W35 cadence GAP: 2026-W34 missing"
    assert "test: 2026-W34 lost" in out["health_detail"]


def test_w36_fails_as_anthropic_billing(tmp_path, monkeypatch):
    rc, out = _run(
        tmp_path, monkeypatch, "2026-W36", ledger=_ledger(tmp_path, *LOST_WEEKS)
    )
    assert rc == crh.EXIT_FAIL
    assert out["health_title"] == "PAGE 2026-W36 anthropic BILLING: 45/1200 samples"
    assert out["health_fingerprint"] == "2026-W36/anthropic/BILLING"
    assert out["health_data_loss"] == "true"
    report = out["health_report"].splitlines()
    assert len(report) > 1  # one finding per line, real newlines
    assert report[0].startswith("2 on-cadence runner(s) in 2026-W36")
    # llama's 750 samples must not carry the week.
    assert "claude-opus-4-8 BILLING: 32/600" in report[0]
    assert "claude-opus-5 BILLING: 13/600" in report[0]
    # Stance died with the same key.
    assert any("stance classifier failed" in line for line in report)


def test_w37_fails_on_a_dead_stance_classifier(tmp_path, monkeypatch):
    rc, out = _run(
        tmp_path, monkeypatch, "2026-W37", ledger=_ledger(tmp_path, *LOST_WEEKS)
    )
    assert rc == crh.EXIT_FAIL
    assert out["health_title"] == "PAGE 2026-W37 stance CLASSIFIER-DEAD: 0/10 cells scored"
    # Sampling itself was fine, and the flag that removes the reassuring
    # "this is about the data" text is for lost samples, not lost stance.
    assert out["health_data_loss"] == "false"


def test_w38_fails_as_anthropic_with_no_samples(tmp_path, monkeypatch):
    rc, out = _run(
        tmp_path, monkeypatch, "2026-W38", ledger=_ledger(tmp_path, *LOST_WEEKS)
    )
    assert rc == crh.EXIT_FAIL
    assert out["health_title"] == "PAGE 2026-W38 anthropic BILLING: 0/1200 samples"
    assert out["health_fingerprint"] == "2026-W38/anthropic/BILLING"
    assert out["health_data_loss"] == "true"


def test_w39_is_clean(tmp_path, monkeypatch):
    rc, out = _run(
        tmp_path, monkeypatch, "2026-W39", ledger=_ledger(tmp_path, *LOST_WEEKS)
    )
    assert rc == crh.EXIT_CLEAN
    assert out["health_title"].startswith("OK 2026-W39")
    # Still said, just not alerted on.
    assert "ref-wifi-unauthorized=12" in out["health_report"]


@pytest.mark.parametrize("week", ["2026-W36", "2026-W38"])
def test_billing_is_recognised_without_a_manifest(tmp_path, monkeypatch, week):
    """The EC2 wrapper judges the run before any manifest is committed to
    main. The run_log alone must be enough to name the failure."""
    out = tmp_path / "out"
    out.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    rc = crh.main([
        week, "--run-log", str(RUN_LOG),
        "--manifest", str(tmp_path / "absent.json"),
        "--gaps", str(_ledger(tmp_path, *LOST_WEEKS)),
    ])
    assert rc == crh.EXIT_FAIL
    assert "anthropic BILLING" in _parse_outputs(out.read_text())["health_title"]


# --- synthetic shapes the production log has not produced yet ----------


def _row(week: str, **kw) -> dict:
    row = {
        "week_id": week,
        "pairs_complete": 0,
        "pairs_skipped": 0,
        "pairs_failed": 0,
        "errors": [],
        "total_samples_written": 0,
        "per_runner_samples": {},
    }
    row.update(kw)
    return row


def _write_log(tmp_path: Path, *rows: dict) -> Path:
    log = tmp_path / "run_log.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return log


def test_a_run_that_wrote_nothing_fails(tmp_path, monkeypatch, capsys):
    """No samples, no failed pairs, no errors: this returned "ok" before
    the roster was recorded."""
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    log = _write_log(
        tmp_path,
        _row("2026-W39", total_samples_written=1338),
        _row("2026-W40", expected_runners=["anthropic/claude-opus-5"],
             expected_samples={"anthropic/claude-opus-5": 600}),
    )
    assert crh.main(["2026-W40", "--run-log", str(log)]) == crh.EXIT_FAIL
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "PAGE 2026-W40 all NO-DATA: 0/600 samples"


def test_a_zero_sample_legacy_row_fails_too(tmp_path):
    entry = crh.aggregate_week([_row("2026-W40")], "2026-W40")
    verdict = crh.coverage_health(entry)
    assert verdict.level == "fail"
    assert verdict.tag == ("all", "NO-DATA", "0 samples")


def test_coverage_is_judged_per_runner_not_on_the_total():
    entry = crh.aggregate_week([_row(
        "2026-W40",
        total_samples_written=1050,
        per_runner_samples={"anthropic/claude-opus-5": 0,
                            "anthropic/claude-opus-4-8": 600,
                            "ollama/llama3.2:3b": 450},
        expected_samples={"anthropic/claude-opus-5": 600,
                          "anthropic/claude-opus-4-8": 600,
                          "ollama/llama3.2:3b": 750},
        runner_halts={"anthropic/claude-opus-5": {
            "error_type": "AuthError", "stage": "prepare", "prompt_id": "*",
            "message": "Error code: 401", "pairs_not_attempted": 30,
        }},
    )], "2026-W40")
    verdict = crh.coverage_health(entry)
    assert verdict.level == "fail"
    # One of two Anthropic runners: the subject is the runner, not the
    # provider, and the class comes from the orchestrator's halt record.
    assert verdict.tag == ("anthropic/claude-opus-5", "AUTH", "0/600 samples")
    assert "halted at prepare" in verdict.detail


def test_a_runner_just_over_half_passes():
    entry = crh.aggregate_week([_row(
        "2026-W40",
        total_samples_written=301,
        per_runner_samples={"openai/gpt-5.5": 301},
        expected_samples={"openai/gpt-5.5": 600},
    )], "2026-W40")
    assert crh.coverage_health(entry).level == "ok"


def test_rejections_count_toward_coverage():
    entry = crh.aggregate_week([_row(
        "2026-W40",
        total_samples_written=280,
        per_runner_samples={"openai/gpt-5.5": 280},
        content_policy_rejections={"openai/gpt-5.5": {"p": 20}},
        expected_samples={"openai/gpt-5.5": 600},
    )], "2026-W40")
    assert crh.coverage_health(entry).level == "ok"


def test_a_resumed_week_is_judged_on_all_its_entries():
    """2026-W19's shape: the second invocation found every pair stored
    and wrote nothing. Last-wins judged the week on the 0."""
    entries = [
        _row("2026-W40", total_samples_written=750, pairs_complete=30,
             per_runner_samples={"ollama/llama3.2:3b": 750},
             expected_samples={"ollama/llama3.2:3b": 750}),
        _row("2026-W40", pairs_skipped=30,
             per_runner_samples={"ollama/llama3.2:3b": 0},
             expected_samples={"ollama/llama3.2:3b": 750}),
    ]
    entry = crh.aggregate_week(entries, "2026-W40")
    assert entry["total_samples_written"] == 750
    assert entry["run_count"] == 2
    assert crh.coverage_health(entry).level == "ok"


def test_a_retry_supersedes_the_failures_it_reran():
    entries = [
        _row("2026-W27", pairs_complete=30, pairs_failed=30,
             total_samples_written=1350,
             per_runner_samples={"ollama/llama3.2:3b": 750, "openai/gpt-5.5": 600},
             errors=[{"provider": "openai", "model_id": "gpt-5.5",
                      "error_type": "UpstreamError", "message": "400"}]),
        _row("2026-W27", pairs_complete=30, pairs_skipped=30,
             per_runner_samples={"ollama/llama3.2:3b": 0, "openai/gpt-5.5": 0}),
    ]
    entry = crh.aggregate_week(entries, "2026-W27")
    assert entry["pairs_failed"] == 0 and entry["errors"] == []
    assert crh.evaluate(entry).level == "ok"


def test_a_partial_retry_keeps_the_other_runners_failures():
    entries = [
        _row("2026-W40", pairs_failed=2, total_samples_written=10,
             per_runner_samples={"anthropic/a": 5, "openai/b": 5},
             errors=[
                 {"provider": "anthropic", "model_id": "a", "error_type": "X", "message": ""},
                 {"provider": "openai", "model_id": "b", "error_type": "Y", "message": ""},
             ]),
        _row("2026-W40", total_samples_written=5, per_runner_samples={"anthropic/a": 5}),
    ]
    entry = crh.aggregate_week(entries, "2026-W40")
    assert [e["model_id"] for e in entry["errors"]] == ["b"]
    assert entry["pairs_failed"] == 2  # cannot be split by runner
    assert crh.evaluate(entry).level == "fail"


def test_a_legacy_policy_rejection_is_not_a_failure():
    """2026-W33: one cybersecurity-flagged 400 recorded as an UpstreamError
    by the pipeline of the day."""
    entry = _row(
        "2026-W33", pairs_complete=59, pairs_failed=1,
        total_samples_written=1332,
        errors=[{"provider": "openai", "model_id": "gpt-5.5",
                 "prompt_id": "ref-wifi-unauthorized",
                 "error_type": "UpstreamError",
                 "message": "Error code: 400 - {'error': {'message': 'This "
                            "content was flagged for possible cybersecurity "
                            "risk."}],
    )
    verdict = crh.evaluate(entry)
    assert verdict.level == "ok"
    assert "declined by the provider" in verdict.detail


def test_a_policy_error_does_not_excuse_other_failed_pairs():
    entry = _row(
        "2026-W33", pairs_failed=2, total_samples_written=1332,
        errors=[{"provider": "openai", "model_id": "gpt-5.5",
                 "error_type": "ContentPolicyError", "message": "blocked"}],
    )
    assert crh.evaluate(entry).level == "fail"


@pytest.mark.parametrize("etype, message, expected", [
    ("BillingError", "", "BILLING"),
    ("UpstreamError", "Your credit balance is too low to access", "BILLING"),
    ("RateLimitError", "insufficient_quota", "BILLING"),
    ("AuthError", "", "AUTH"),
    ("UpstreamError", "Error code: 403 - forbidden", "AUTH"),
    ("ContentPolicyError", "", "POLICY"),
    ("RateLimitError", "slow down", "RATE-LIMIT"),
    ("integrity", "digest mismatch", "INTEGRITY"),
    ("UpstreamError", "Error code: 500", "UPSTREAM"),
    ("", "", "ERROR"),
])
def test_error_classes(etype, message, expected):
    assert crh.classify_error(etype, message) == expected


def test_error_summary_prefers_the_uncapped_count():
    entry = _row(
        "2026-W40",
        errors=[{"provider": "anthropic", "model_id": "a",
                 "error_type": "BillingError", "message": ""}] * 50,
        error_summary={"anthropic/a": {"BillingError": 30},
                       "anthropic/b": {"BillingError": 30}},
    )
    text = crh.error_summary_text(entry)
    assert "anthropic/a BillingError=30" in text
    assert "anthropic/b BillingError=30" in text


def test_error_summary_marks_a_capped_legacy_list():
    entries = [_row(
        "2026-W36",
        pairs_failed=59,
        errors=[{"provider": "anthropic", "model_id": "a",
                 "error_type": "UpstreamError",
                 "message": "credit balance is too low"}] * 50,
    )]
    text = crh.error_summary_text(crh.aggregate_week(entries, "2026-W36"))
    assert "UpstreamError=50 [BILLING]" in text
    assert "first 50 errors only" in text


# --- gap ledger ----------------------------------------------------------


def test_an_absent_ledger_is_empty(tmp_path):
    assert crh.load_gap_ledger(str(tmp_path / "gaps.jsonl")) == ({}, [])


def test_the_ledger_skips_and_reports_bad_lines(tmp_path):
    path = tmp_path / "gaps.jsonl"
    path.write_text(
        '{"week_id": "2026-W34", "reason": "timeout"}\n'
        "not json\n"
        '{"reason": "no week"}\n'
        "\n"
        '["2026-W30"]\n',
        encoding="utf-8",
    )
    ledger, malformed = crh.load_gap_ledger(str(path))
    assert list(ledger) == ["2026-W34"]
    assert malformed == [2, 3, 5]


def test_an_acknowledged_older_gap_is_not_announced():
    weeks = ("2026-W28", "2026-W29", "2026-W32", "2026-W33")
    entries = [_row(w) for w in weeks]
    acknowledged = {"2026-W30": [{"reason": "capacity"}],
                    "2026-W31": [{"reason": "capacity"}]}
    assert crh.cadence_health(entries, "2026-W33").level == "warn"
    verdict = crh.cadence_health(entries, "2026-W33", acknowledged)
    assert verdict.level == "ok"
    assert "capacity" in verdict.detail


def test_an_unacknowledged_previous_week_still_fails():
    entries = [_row(w) for w in ("2026-W32", "2026-W33", "2026-W35")]
    verdict = crh.cadence_health(entries, "2026-W35", {"2026-W30": [{}]})
    assert verdict.level == "fail"
    assert verdict.tag == ("cadence", "GAP", "2026-W34 missing")


# --- stance --------------------------------------------------------------


def _stance_manifest(cells: list[tuple[str, str, float | None]]) -> dict:
    return {
        "prompts": [{"prompt_id": "pol-a", "axis": "political"},
                    {"prompt_id": "pol-b", "axis": "political"},
                    {"prompt_id": "sci-a", "axis": "scientific"}],
        "metrics": [
            {"prompt_id": pid, "model_id": model, "stance": "na",
             "stance_confidence": conf}
            for model, pid, conf in cells
        ] + [{"prompt_id": "sci-a", "model_id": "m", "stance": "na",
              "stance_confidence": 0.0}],
    }


def test_stance_disabled_is_not_a_dead_classifier():
    manifest = _stance_manifest([("m", "pol-a", None), ("m", "pol-b", None)])
    assert crh.stance_health(manifest, "2026-W40").level == "ok"


def test_stance_with_nothing_to_classify_is_not_dead():
    manifest = _stance_manifest([("m", "pol-a", 1.0), ("m", "pol-b", 1.0)])
    assert crh.stance_health(manifest, "2026-W40").level == "ok"


def test_stance_mostly_failed_warns():
    manifest = _stance_manifest([("m", "pol-a", 0.0), ("m", "pol-b", 0.0),
                                 ("m", "pol-c", 0.85)])
    manifest["prompts"].append({"prompt_id": "pol-c", "axis": "political"})
    assert crh.stance_health(manifest, "2026-W40").level == "warn"


def test_no_manifest_skips_the_stance_check():
    assert crh.stance_health(None, "2026-W40") == crh.RunHealth("ok", "")


def test_a_corrupt_manifest_is_reported_not_raised(tmp_path, capsys):
    log = _write_log(tmp_path, _row("2026-W40", total_samples_written=750,
                                    per_runner_samples={"ollama/x": 750}))
    bad = tmp_path / "manifest.json"
    bad.write_text("{not json", encoding="utf-8")
    rc = crh.main(["2026-W40", "--run-log", str(log), "--manifest", str(bad)])
    assert rc == crh.EXIT_WARN
    assert "could not be read" in capsys.readouterr().out


# --- titles --------------------------------------------------------------


def test_titles_fit_an_sns_subject():
    verdict = crh.RunHealth(
        "fail", "x", ("anthropic", "BILLING", "0/1200 samples " + "y" * 200)
    )
    title, fingerprint = crh.alert_title(verdict, "2026-W40")
    assert len(title) <= 85 and title.isascii()
    assert fingerprint == "2026-W40/anthropic/BILLING"


def test_an_untagged_verdict_still_gets_a_title():
    title, fingerprint = crh.alert_title(
        crh.RunHealth("warn", "something odd | more"), "2026-W40"
    )
    assert title == "WARN 2026-W40: something odd"
    assert fingerprint == "2026-W40/pipeline/WARN"


# --- the writer side -----------------------------------------------------


def test_run_log_records_the_roster_and_an_uncapped_error_summary(tmp_path):
    from meridian.config import PipelineConfig, RunnerSpec, SamplingSpec
    from meridian.pipeline.run_log import append_run_log, read_run_log
    from meridian.sampling.orchestrator import PairError, RunOutcome

    outcome = RunOutcome(
        week_id="2026-W40",
        total_samples_written=0,
        pairs_complete=0,
        pairs_skipped=0,
        pairs_failed=60,
        per_runner_samples={"anthropic/a": 0, "anthropic/b": 0},
        errors=[
            PairError(provider="anthropic", model_id=m, prompt_id=f"p{i}",
                      error_type="BillingError", message="credit")
            for m in ("a", "b") for i in range(30)
        ],
    )
    log = tmp_path / "run_log.jsonl"
    now = datetime(2026, 10, 5, 9, tzinfo=timezone.utc)
    append_run_log(
        log, started_at=now, finished_at=now, week_id="2026-W40",
        config=PipelineConfig(sampling=SamplingSpec(), runners=[
            RunnerSpec(provider="anthropic", model_id="a", enabled=True),
        ]),
        outcome=outcome, estimated_cost_usd=1.0, actual_cost_usd=0.0,
        expected_samples={"anthropic/b": 600, "anthropic/a": 600},
        stored_samples={},
    )
    [entry] = read_run_log(log)
    assert len(entry.errors) == 50
    assert entry.error_summary == {"anthropic/a": {"BillingError": 30},
                                   "anthropic/b": {"BillingError": 30}}
    assert entry.expected_runners == ["anthropic/a", "anthropic/b"]

    # And the check reads it back into a titled verdict.
    raw = [json.loads(line) for line in log.read_text().splitlines()]
    verdict = crh.coverage_health(crh.aggregate_week(raw, "2026-W40"))
    assert verdict.tag == ("all", "NO-DATA", "0/1200 samples")
