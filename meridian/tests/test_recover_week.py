"""``cli recover-week``: a killed week's record, reconstructed honestly.

2026-W34 was killed at the SSM execution timeout before it could write a
run_log entry, a manifest or a snapshot. These tests pin what the
reconstruction may and may not do, all in a tmp repo:

* a dry run writes nothing;
* ``--write`` produces a manifest marked partial with per-runner coverage
  and notes, the responses snapshot, and exactly one run_log entry with
  ``recovery: true`` whose pair counts are what is on disk;
* it refuses, before writing anything, when the week already has a
  run_log entry or a manifest (locally or in S3), when the local run_log
  differs from the S3 copy, when the week is not in the past, and when
  the raw store holds a model the roster did not run;
* the S3 upload never moves ``manifests/latest.json``;
* the health check, given the ledger's ``partial`` record for the week,
  warns on the result rather than paging, and fails without it.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

from meridian.corpus import load_corpus
from meridian.pipeline import cli as cli_module
from meridian.pipeline.run_log import read_run_log
from meridian.runners.base import Sample
from meridian.storage import LocalSampleStore

WEEK = "2026-W34"
BUCKET = "meridian-recovery-test"
REGION = "us-east-1"

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_run_health.py"
_spec = importlib.util.spec_from_file_location("check_run_health_recover", _SCRIPT)
crh = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(crh)

CAUSE = "was killed at the one-hour SSM execution timeout"


def _sample(provider: str, model_id: str, prompt_id: str, i: int, minute: int) -> Sample:
    return Sample(
        prompt_id=prompt_id,
        model_id=model_id,
        provider=provider,
        request_index=i,
        temperature=1.0,
        max_tokens=1024,
        text=f"A substantive answer number {i} about {prompt_id}.",
        model_version_string=f"{model_id}-test",
        stop_reason="end_turn",
        latency_ms=1,
        captured_at=datetime(2026, 8, 24, 9, 5, tzinfo=timezone.utc)
        + timedelta(minutes=minute),
        input_tokens=50,
        output_tokens=100,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A tmp repo: three prompts, llama every week (5 per pair) and
    claude-opus-5 on even weeks (4 per pair, it rejects temperature 0).
    claude-opus-5 has one complete prompt, one cut short at 2 of 4, and
    one never sampled, the shape of the real 2026-W34."""
    (tmp_path / "data").mkdir()
    (tmp_path / "site" / "fixtures").mkdir(parents=True)
    monkeypatch.setattr(cli_module, "REPO_ROOT", tmp_path)

    full = load_corpus()
    prompts = full.public()[:3]
    corpus = full.model_copy(update={"prompts": prompts})
    monkeypatch.setattr(cli_module, "load_corpus", lambda: corpus)

    store = LocalSampleStore(tmp_path / "data" / "raw")
    for k, p in enumerate(prompts):
        for i in range(5):
            store.append(WEEK, "llama3.2:3b", p.id,
                         _sample("ollama", "llama3.2:3b", p.id, i, k * 5 + i))
    for i, n in ((0, 4), (1, 2)):
        for j in range(n):
            store.append(WEEK, "claude-opus-5", prompts[i].id,
                         _sample("anthropic", "claude-opus-5", prompts[i].id, j, 30 + i * 4 + j))
    return tmp_path


def _config(repo: Path, *, s3: bool = False) -> Path:
    cfg = {
        "sampling": {
            "n_default_temp": 4, "n_zero_temp": 1, "max_tokens": 64,
            "concurrency_per_provider": 1,
        },
        "storage": {"raw_dir": "data/raw"},
        "runners": [
            {"provider": "ollama", "model_id": "llama3.2:3b",
             "enabled": True, "cadence": "every_week"},
            {"provider": "anthropic", "model_id": "claude-opus-5",
             "enabled": True, "cadence": "even_weeks", "max_tokens": 8192},
            {"provider": "openai", "model_id": "gpt-5.5",
             "enabled": True, "cadence": "odd_weeks"},
        ],
    }
    if s3:
        cfg["storage"]["s3"] = {
            "bucket": BUCKET, "region": REGION, "prefix": "meridian/",
            "publish_latest_pointer": True,
        }
    path = repo / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def _run(repo: Path, *extra: str, s3: bool = False) -> int:
    return cli_module.main([
        "--config", str(_config(repo, s3=s3)),
        "recover-week", "--week", WEEK,
        "--archived-on", "2026-10-05", "--cause", CAUSE, *extra,
    ])


def _written(repo: Path) -> list[Path]:
    return [
        p for p in (
            repo / "data" / "run_log.jsonl",
            repo / "data" / "manifests" / f"{WEEK}.json",
            repo / "site" / "fixtures" / f"manifest-{WEEK}.json",
            repo / "data" / "snapshots" / WEEK / "responses.jsonl.gz",
        ) if p.exists()
    ]


def test_dry_run_writes_nothing(repo: Path, capsys):
    assert _run(repo, "--no-archive") == 0
    out = capsys.readouterr().out
    assert "dry run: nothing written" in out
    assert "anthropic/claude-opus-5" in out and "6/12" in out
    assert "pairs_complete=4 pairs_failed=2" in out
    assert _written(repo) == []


def test_write_builds_partial_manifest_snapshot_and_one_recovery_entry(repo: Path):
    assert _run(repo, "--write", "--no-archive") == 0

    data = repo / "data" / "manifests" / f"{WEEK}.json"
    fixture = repo / "site" / "fixtures" / f"manifest-{WEEK}.json"
    assert data.read_bytes() == fixture.read_bytes()
    manifest = json.loads(data.read_text())

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "site" / "src"))
    from schema import Manifest
    parsed = Manifest.model_validate(manifest)
    assert parsed.partial is True
    cov = {c.model_id: c for c in parsed.coverage}
    assert cov["claude-opus-5"].status == "partial"
    assert (cov["claude-opus-5"].captured_samples, cov["claude-opus-5"].expected_samples) == (6, 12)
    assert len(cov["claude-opus-5"].partial_prompts) == 1
    assert len(cov["claude-opus-5"].missing_prompts) == 1
    assert cov["llama3.2:3b"].status == "complete"
    assert any(CAUSE in n for n in parsed.notes)
    assert any("2026-10-05" in n for n in parsed.notes)
    # The missing prompt has no metric record; the cut-short one has n=2.
    opus = {m.prompt_id: m.n_samples for m in parsed.metrics if m.model_id == "claude-opus-5"}
    assert sorted(opus.values()) == [2, 4]

    with gzip.open(repo / "data" / "snapshots" / WEEK / "responses.jsonl.gz", "rt") as fh:
        assert sum(1 for _ in fh) == 15 + 6

    [entry] = read_run_log(repo / "data" / "run_log.jsonl")
    assert entry.recovery is True
    assert entry.week_id == WEEK
    assert (entry.pairs_complete, entry.pairs_failed, entry.pairs_skipped) == (4, 2, 0)
    assert entry.per_runner_samples == {
        "ollama/llama3.2:3b": 15, "anthropic/claude-opus-5": 6,
    }
    assert entry.expected_samples == {
        "anthropic/claude-opus-5": 12, "ollama/llama3.2:3b": 15,
    }
    assert entry.errors == []
    halt = entry.runner_halts["anthropic/claude-opus-5"]
    assert halt["error_type"] == "ExecutionTimeout"
    assert halt["pairs_not_attempted"] == 1
    assert entry.started_at.startswith("2026-08-24T09:05")
    assert "recovery: true" in entry.note and "2026-08-24" in entry.note
    assert "archived to S3 2026-10-05" in entry.note
    raw = json.loads((repo / "data" / "run_log.jsonl").read_text())
    assert raw["recovery"] is True


def test_reconstructed_row_names_the_weeks_roster_and_config_not_todays(repo: Path):
    """The row is permanent, so it must describe the week, not the rebuild.

    The config gains a runner whose ``first_week`` is after the week (as
    claude-opus-5-5 and gpt-6-astra were added on 2026-10-06, long after
    2026-W34). The row's ``runners`` is the week's roster only, its
    ``config_hash`` is the one passed in rather than today's, and the
    note says which fields describe the rebuild."""
    cfg_path = _config(repo)
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["runners"].append({
        "provider": "anthropic", "model_id": "claude-later", "enabled": True,
        "cadence": "every_week", "first_week": "2026-W41",
    })
    cfg_path.write_text(yaml.safe_dump(cfg))
    rc = cli_module.main([
        "--config", str(cfg_path), "recover-week", "--week", WEEK,
        "--archived-on", "2026-10-05", "--cause", CAUSE,
        "--config-hash", "e78efdfab25cd47d", "--write", "--no-archive",
    ])
    assert rc == 0
    [entry] = read_run_log(repo / "data" / "run_log.jsonl")
    roster = sorted(
        f"{s.provider}/{s.model_id}"
        for s in cli_module._enabled_specs_for_week(cli_module.load_config(cfg_path), WEEK)
    )
    assert entry.runners == roster == ["anthropic/claude-opus-5", "ollama/llama3.2:3b"]
    assert "anthropic/claude-later" not in entry.runners
    assert "openai/gpt-5.5" not in entry.runners  # off cadence in an even week
    assert entry.config_hash == "e78efdfab25cd47d"
    assert "host and pid are those of the recover-week invocation" in entry.note
    assert "config_hash e78efdfab25cd47d" in entry.note


def test_reconstructed_row_without_a_known_hash_records_null(repo: Path):
    assert _run(repo, "--write", "--no-archive") == 0
    raw = json.loads((repo / "data" / "run_log.jsonl").read_text())
    assert raw["config_hash"] is None
    assert "config_hash is null" in raw["note"]


def test_refuses_a_malformed_config_hash(repo: Path, capsys):
    assert _run(repo, "--config-hash", "not-a-hash", "--no-archive") == 2
    assert "16-hex-digit" in capsys.readouterr().err
    assert _written(repo) == []


def test_refuses_a_week_that_already_logged_itself(repo: Path, capsys):
    assert _run(repo, "--write", "--no-archive") == 0
    before = {p: p.read_bytes() for p in _written(repo)}
    assert _run(repo, "--write", "--no-archive") == 2
    assert "already has an entry" in capsys.readouterr().err
    assert {p: p.read_bytes() for p in _written(repo)} == before


def test_refuses_when_a_manifest_exists(repo: Path, capsys):
    path = repo / "data" / "manifests" / f"{WEEK}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    assert _run(repo, "--write", "--no-archive") == 2
    assert "already exists" in capsys.readouterr().err
    assert path.read_text() == "{}"
    assert not (repo / "data" / "run_log.jsonl").exists()


def test_refuses_the_current_week(repo: Path, capsys):
    from meridian.sampling.weeks import iso_week_for
    rc = cli_module.main([
        "--config", str(_config(repo)), "recover-week", "--week", iso_week_for(),
        "--archived-on", "2026-10-05", "--cause", CAUSE, "--write", "--no-archive",
    ])
    assert rc == 2
    assert "not a past week" in capsys.readouterr().err


def test_refuses_samples_from_a_model_the_roster_did_not_run(repo: Path, capsys):
    store = LocalSampleStore(repo / "data" / "raw")
    pid = load_corpus().public()[0].id
    store.append(WEEK, "gpt-5.5", pid, _sample("openai", "gpt-5.5", pid, 0, 0))
    assert _run(repo, "--write", "--no-archive") == 2
    assert "gpt-5.5" in capsys.readouterr().err
    assert _written(repo) == []


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield client


def _keys(client) -> set[str]:
    return {o["Key"] for o in client.list_objects_v2(Bucket=BUCKET).get("Contents", [])}


def test_archive_uploads_without_moving_latest_pointer(repo: Path, s3):
    log = repo / "data" / "run_log.jsonl"
    log.write_text('{"week_id": "2026-W33"}\n')
    s3.put_object(Bucket=BUCKET, Key="meridian/run_log.jsonl", Body=log.read_bytes())
    s3.put_object(Bucket=BUCKET, Key="meridian/manifests/latest.json", Body=b"W40")

    assert _run(repo, "--write", s3=True) == 0

    keys = _keys(s3)
    assert f"meridian/manifests/{WEEK}.json" in keys
    assert f"meridian/snapshots/{WEEK}/responses.jsonl.gz" in keys
    latest = s3.get_object(Bucket=BUCKET, Key="meridian/manifests/latest.json")
    assert latest["Body"].read() == b"W40"
    remote = s3.get_object(Bucket=BUCKET, Key="meridian/run_log.jsonl")["Body"].read()
    assert remote == log.read_bytes()
    assert remote.decode().splitlines()[0] == '{"week_id": "2026-W33"}'
    assert json.loads(remote.decode().splitlines()[1])["recovery"] is True
    # Raw samples are not re-uploaded: the archive is the source.
    assert not any(k.startswith("meridian/raw/") for k in keys)


def test_refuses_when_s3_already_has_the_manifest(repo: Path, s3, capsys):
    s3.put_object(Bucket=BUCKET, Key=f"meridian/manifests/{WEEK}.json", Body=b"{}")
    assert _run(repo, "--write", s3=True) == 2
    assert "s3 already holds" in capsys.readouterr().err
    assert _written(repo) == []


def test_refuses_when_local_run_log_differs_from_s3(repo: Path, s3, capsys):
    (repo / "data" / "run_log.jsonl").write_text('{"week_id": "2026-W33"}\n')
    s3.put_object(Bucket=BUCKET, Key="meridian/run_log.jsonl",
                  Body=b'{"week_id": "2026-W33"}\n{"week_id": "2026-W41"}\n')
    assert _run(repo, "--write", s3=True) == 2
    assert "differs from the run_log in S3" in capsys.readouterr().err
    assert not (repo / "data" / "manifests").exists()


def _health(repo: Path, ledger: list[dict]) -> int:
    gaps = repo / "data" / "gaps.jsonl"
    gaps.write_text("".join(json.dumps(r) + "\n" for r in ledger))
    return crh.main([WEEK, "--run-log", str(repo / "data" / "run_log.jsonl"),
                     "--gaps", str(gaps)])


def test_health_check_pages_without_the_ledger_and_warns_with_it(repo: Path, capsys):
    assert _run(repo, "--write", "--no-archive") == 0
    capsys.readouterr()

    assert _health(repo, []) == crh.EXIT_FAIL
    assert "PAGE" in capsys.readouterr().out

    ledger = [{
        "week_id": WEEK, "scope": "anthropic/claude-opus-5", "kind": "partial",
        "reason": "killed at the SSM timeout", "recorded_at": "2026-10-07",
    }]
    assert _health(repo, ledger) == crh.EXIT_WARN
    out = capsys.readouterr().out
    assert "WARN" in out and "killed at the SSM timeout" in out

    # A ledger line for a different runner acknowledges nothing.
    other = [dict(ledger[0], scope="ollama/llama3.2:3b")]
    assert _health(repo, other) == crh.EXIT_FAIL
    # A note acknowledges nothing either.
    note = [dict(ledger[0], kind="note")]
    assert _health(repo, note) == crh.EXIT_FAIL


def test_live_run_rows_do_not_gain_a_recovery_key(tmp_path: Path):
    """``recovery`` is written only when true, so a live run's row is
    byte-for-byte what it was before reconstructions existed, and an old
    row without it still reads back as False."""
    from meridian.config import load_config
    from meridian.pipeline.run_log import append_run_log
    from meridian.sampling.orchestrator import RunOutcome

    log = tmp_path / "run_log.jsonl"
    now = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
    append_run_log(
        log, started_at=now, finished_at=now, week_id="2026-W41",
        config=load_config(None), outcome=RunOutcome(week_id="2026-W41"),
        estimated_cost_usd=0.0, actual_cost_usd=0.0,
    )
    assert "recovery" not in json.loads(log.read_text())
    [entry] = read_run_log(log)
    assert entry.recovery is False


def test_acknowledged_runners_matches_scope_and_kind():
    runners = ["anthropic/claude-opus-4-8", "anthropic/claude-opus-5", "ollama/llama3.2:3b"]
    rec = lambda **kw: dict({"week_id": WEEK, "reason": "r"}, **kw)  # noqa: E731
    assert set(crh.acknowledged_runners([rec(scope="all")], runners)) == set(runners)
    assert set(crh.acknowledged_runners([rec()], runners)) == set(runners)
    assert set(crh.acknowledged_runners([rec(scope="anthropic", kind="lost")], runners)) == {
        "anthropic/claude-opus-4-8", "anthropic/claude-opus-5",
    }
    assert crh.acknowledged_runners([rec(scope="stance", kind="lost")], runners) == {}
    assert crh.acknowledged_runners([rec(scope="all", kind="degraded")], runners) == {}
    assert crh.stance_acknowledged([rec(scope="stance", kind="lost")]) == "r"
    assert crh.stance_acknowledged([rec(scope="stance", kind="note")]) is None


def test_dead_stance_warns_only_when_the_ledger_records_it():
    manifest = {
        "prompts": [{"prompt_id": "p", "axis": "political"}],
        "metrics": [{"prompt_id": "p", "model_id": "m", "stance": "na",
                     "stance_confidence": 0.0}],
    }
    assert crh.stance_health(manifest, WEEK).level == "fail"
    assert crh.stance_health(manifest, WEEK, "classifier on empty balance").level == "warn"


def test_recovery_wrapper_parses_and_rejects_an_unknown_step(tmp_path: Path):
    """scripts/recovery.sh is what SSM runs; a syntax error there is found
    on the instance at a dollar an hour. Also pins that its W34 facts are
    the ones the gap ledger states."""
    import os
    import subprocess

    script = Path(__file__).resolve().parents[2] / "scripts" / "recovery.sh"
    assert os.access(script, os.X_OK)
    subprocess.run(["bash", "-n", str(script)], check=True)
    env = dict(os.environ, REPO_DIR=str(tmp_path), LOG_DIR=str(tmp_path / "logs"))
    proc = subprocess.run(["bash", str(script), "bogus"], env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 64
    assert "usage:" in proc.stdout + proc.stderr
    text = script.read_text()
    assert 'W34_ARCHIVED_ON="2026-10-05"' in text
    assert 'STANCE_WEEKS="2026-W36,2026-W37,2026-W38"' in text


def _git(cwd: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _runlog_check(repo: Path, tmp_path: Path):
    import os
    import subprocess

    script = Path(__file__).resolve().parents[2] / "scripts" / "recovery.sh"
    env = dict(os.environ, REPO_DIR=str(repo), LOG_DIR=str(tmp_path / "logs"))
    return subprocess.run(["bash", str(script), "runlog-check"], env=env,
                          capture_output=True, text=True)


@pytest.fixture
def instance_checkout(tmp_path: Path) -> tuple[Path, Path]:
    """An origin with one run_log row, and an instance clone of it."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(origin), str(seed))
    (seed / "data").mkdir()
    (seed / "data" / "run_log.jsonl").write_text('{"week_id": "2026-W39"}\n')
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "HEAD:main")
    inst = tmp_path / "instance"
    _git(tmp_path, "clone", str(origin), str(inst))
    return seed, inst


def test_preflight_runlog_check_accepts_the_normal_post_run_state(
    instance_checkout, tmp_path: Path,
):
    """After a weekly run the instance's run_log is HEAD plus that week's
    row, uncommitted, and the publish workflow has since committed the
    same row to main. That is the steady state and must not refuse."""
    seed, inst = instance_checkout
    row = '{"week_id": "2026-W40"}\n'
    with (inst / "data" / "run_log.jsonl").open("a") as fh:
        fh.write(row)
    with (seed / "data" / "run_log.jsonl").open("a") as fh:
        fh.write(row)
    _git(seed, "commit", "-am", "publish W40")
    _git(seed, "push", "origin", "HEAD:main")
    proc = _runlog_check(inst, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "matches origin/main" in proc.stdout

    # Main moved further ahead (another publish): still fine.
    with (seed / "data" / "run_log.jsonl").open("a") as fh:
        fh.write('{"week_id": "2026-W41"}\n')
    _git(seed, "commit", "-am", "publish W41")
    _git(seed, "push", "origin", "HEAD:main")
    proc = _runlog_check(inst, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ahead of the local copy" in proc.stdout


def test_preflight_runlog_check_refuses_rows_main_does_not_have(
    instance_checkout, tmp_path: Path,
):
    _seed, inst = instance_checkout
    with (inst / "data" / "run_log.jsonl").open("a") as fh:
        fh.write('{"week_id": "2026-W34", "recovery": true}\n')
    proc = _runlog_check(inst, tmp_path)
    assert proc.returncode == 2
    assert "rows origin/main does not have" in proc.stdout
