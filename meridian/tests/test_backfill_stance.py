"""scripts/backfill_stance.py: re-classify unmeasured stance, graft, prove it.

2026-W36 to W38 published every stance-bearing cell whose classifier call
failed as ``na`` at confidence 0.0. The correction re-classifies exactly
those cells from the published responses and grafts three fields back.
These tests pin that it touches nothing else, refuses a half-done
correction, and is idempotent. The real manifests and snapshots are read,
never written: every write goes to tmp_path copies.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from meridian.analysis.stance import StanceResult
from meridian.corpus import load_corpus
from meridian.runners.base import Sample

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "backfill_stance.py"
_spec = importlib.util.spec_from_file_location("backfill_stance", _SCRIPT)
bs = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(bs)

sys.path.insert(0, str(ROOT / "site" / "src"))
from schema import Manifest  # noqa: E402

MANIFESTS = ROOT / "data" / "manifests"
SNAPSHOTS = ROOT / "data" / "snapshots"


def _published(week: str) -> dict:
    return json.loads((MANIFESTS / f"{week}.json").read_text())


def _results(manifest: dict, *, stance: str = "neutral", conf: float = 0.85,
             reason: str | None = None) -> list[dict]:
    week = manifest["snapshot"]["week_id"]
    return [
        {
            "week_id": week, "prompt_id": pid, "model_id": mid,
            "stance": stance, "stance_confidence": conf, "stance_reason": reason,
            "samples": 20, "response_sha256": "0" * 64,
            "classifier": "anthropic/claude-haiku-4-5-20251001",
            "classified_at": "2026-10-08T10:00:00+00:00",
        }
        for pid, mid in bs.unmeasured_cells(manifest)
    ]


@pytest.mark.parametrize("week, expected", [
    ("2026-W36", {"claude-opus-4-8": 2, "claude-opus-5": 1, "llama3.2:3b": 10}),
    ("2026-W37", {"gpt-5.5": 10, "llama3.2:3b": 9}),
    ("2026-W38", {"llama3.2:3b": 10}),
])
def test_finds_exactly_the_published_unmeasured_cells(week, expected):
    cells = bs.unmeasured_cells(_published(week))
    counts: dict[str, int] = {}
    for _pid, mid in cells:
        counts[mid] = counts.get(mid, 0) + 1
    assert counts == expected


def test_scored_weeks_have_nothing_to_correct():
    for week in ("2026-W35", "2026-W39", "2026-W40"):
        assert bs.unmeasured_cells(_published(week)) == []


def test_graft_changes_only_stance_fields_of_target_cells():
    before = _published("2026-W37")
    results = _results(before)
    correction = {"date": "2026-10-08", "fields": list(bs.STANCE_FIELDS),
                  "summary": "test", "report": "/reports/x/"}
    after, changes = bs.graft_week(before, results, correction)

    assert len(changes) == 19
    assert before == _published("2026-W37"), "graft must not mutate its input"
    bs.verify_graft(before, after, results)
    Manifest.model_validate(after)
    assert after["corrections"] == [dict(correction, cells=19)]
    # The one W37 llama cell scored from the cache is untouched.
    scored = [m for m in after["metrics"]
              if m["model_id"] == "llama3.2:3b" and m["stance_confidence"] == 0.85
              and (m["prompt_id"], m["model_id"]) not in
              {(r["prompt_id"], r["model_id"]) for r in results}]
    assert len(scored) == 1
    assert bs.strip_stance(before) == bs.strip_stance(after)
    assert after["history"] == before["history"]
    assert bs.unmeasured_cells(after) == []


def test_verify_catches_any_other_change():
    before = _published("2026-W38")
    results = _results(before)
    after, _ = bs.graft_week(before, results, None)
    after["metrics"][0]["refusal_rate"] = 0.123456
    with pytest.raises(AssertionError, match="other than stance"):
        bs.verify_graft(before, after, results)

    after, _ = bs.graft_week(before, results, None)
    untargeted = next(
        m for m in after["metrics"]
        if (m["prompt_id"], m["model_id"]) not in
        {(r["prompt_id"], r["model_id"]) for r in results}
    )
    untargeted["stance"] = "pro"
    with pytest.raises(AssertionError, match="untargeted"):
        bs.verify_graft(before, after, results)


def test_graft_refuses_incomplete_stray_or_failed_results():
    before = _published("2026-W36")
    results = _results(before)
    with pytest.raises(ValueError, match="no result"):
        bs.graft_week(before, results[1:], None)
    stray = dict(results[0], prompt_id="ctrl-photosynthesis")
    with pytest.raises(ValueError, match="not unmeasured"):
        bs.graft_week(before, results + [stray], None)
    failed = [dict(results[0], stance="na", stance_confidence=0.0,
                   stance_reason="runner-error")] + results[1:]
    with pytest.raises(ValueError, match="failed call"):
        bs.graft_week(before, failed, None)


def _tmp_tree(tmp_path: Path, weeks: list[str]) -> tuple[Path, Path]:
    manifests = tmp_path / "manifests"
    fixtures = tmp_path / "fixtures"
    manifests.mkdir()
    fixtures.mkdir()
    for w in weeks:
        shutil.copy(MANIFESTS / f"{w}.json", manifests / f"{w}.json")
        shutil.copy(MANIFESTS / f"{w}.json", fixtures / f"manifest-{w}.json")
    return manifests, fixtures


def _graft(results: Path, manifests: Path, fixtures: Path, mode: str) -> int:
    return bs.main([
        "graft", "--results", str(results), "--date", "2026-10-08",
        "--manifests-dir", str(manifests), "--fixtures-dir", str(fixtures), mode,
    ])


def test_graft_cli_dry_run_then_write_then_idempotent(tmp_path: Path, capsys):
    weeks = ["2026-W36", "2026-W37", "2026-W38"]
    manifests, fixtures = _tmp_tree(tmp_path, weeks)
    results = tmp_path / "stance-results.jsonl"
    results.write_text("".join(
        json.dumps(r) + "\n" for w in weeks for r in _results(_published(w))
    ))
    originals = {p: p.read_bytes() for p in list(manifests.iterdir()) + list(fixtures.iterdir())}

    assert _graft(results, manifests, fixtures, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "| 2026-W37 | gpt-5.5 |" in out and "dry run" in out
    # The public report carries the digest of each classified response,
    # since the results file itself is not published.
    assert "Response SHA-256 |" in out and f"`{'0' * 64}` |" in out
    assert {p: p.read_bytes() for p in originals} == originals

    assert _graft(results, manifests, fixtures, "--write") == 0
    capsys.readouterr()
    for w in weeks:
        a = (manifests / f"{w}.json").read_bytes()
        assert a == (fixtures / f"manifest-{w}.json").read_bytes()
        corrected = json.loads(a)
        assert corrected["corrections"][0]["report"] == (
            "/reports/2026-10-08-stance-classifier-correction/"
        )
        assert bs.strip_stance(corrected) == bs.strip_stance(_published(w))

    snapshot = {p: p.read_bytes() for p in originals}
    assert _graft(results, manifests, fixtures, "--write") == 2
    assert "not unmeasured" in capsys.readouterr().err
    assert {p: p.read_bytes() for p in originals} == snapshot


def test_graft_refuses_when_the_two_copies_differ(tmp_path: Path, capsys):
    manifests, fixtures = _tmp_tree(tmp_path, ["2026-W38"])
    (fixtures / "manifest-2026-W38.json").write_text("{}\n")
    results = tmp_path / "r.jsonl"
    results.write_text("".join(json.dumps(r) + "\n" for r in _results(_published("2026-W38"))))
    assert _graft(results, manifests, fixtures, "--write") == 2
    assert "identical" in capsys.readouterr().err


class _FakeClassifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def classify(self, *, prompt_id, axis, prompt_text, response_text):
        self.calls.append(prompt_id)
        return StanceResult("pro", 0.85, None)


def _write_snapshot(path: Path, samples: list[Sample]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for s in samples:
            fh.write(s.model_dump_json() + "\n")


def _sample(pid: str, mid: str, i: int, text: str) -> Sample:
    return Sample(
        prompt_id=pid, model_id=mid, provider="ollama", request_index=i,
        temperature=1.0, max_tokens=1024, text=text,
        model_version_string="t", latency_ms=1,
        captured_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
    )


def test_classify_cells_uses_the_pipelines_representative_response(tmp_path: Path):
    import asyncio

    prompts = {p.id: p for p in load_corpus().all()}
    pids = [p.id for p in load_corpus().by_axis("political")][:2]
    samples = [
        _sample(pids[0], "llama3.2:3b", 0, "Short substantive answer."),
        _sample(pids[0], "llama3.2:3b", 1, "A much longer substantive answer with more to say."),
        _sample(pids[1], "llama3.2:3b", 0, "I can't help with that."),
    ]
    snap = tmp_path / "snap" / "responses.jsonl.gz"
    _write_snapshot(snap, samples)
    store = bs.rehydrate_week(snap, tmp_path / "store", "2026-W38")
    fake = _FakeClassifier()
    out = asyncio.run(bs.classify_cells(
        fake, store, prompts, "2026-W38",
        [(pids[0], "llama3.2:3b"), (pids[1], "llama3.2:3b")],
        classifier_name="anthropic/claude-haiku-4-5-20251001",
    ))
    assert fake.calls == [pids[0]]
    assert out[0]["stance"] == "pro" and out[0]["stance_reason"] is None
    import hashlib
    assert out[0]["response_sha256"] == hashlib.sha256(
        samples[1].text.encode()).hexdigest()
    assert out[1]["stance"] == "na" and out[1]["stance_confidence"] == 1.0
    assert out[1]["stance_reason"] == "no-substantive-response"
    assert out[0]["samples"] == 2


def test_classify_command_end_to_end_on_the_published_w38(tmp_path: Path, monkeypatch):
    """Real W38 manifest and snapshot (read only), fake classifier."""
    from meridian.pipeline import stance_runner

    fake = _FakeClassifier()
    monkeypatch.setattr(stance_runner, "build_stance_classifier",
                        lambda spec, repo_root: fake)
    monkeypatch.delenv("MERIDIAN_SECRETS_SSM", raising=False)
    out = tmp_path / "out"
    rc = bs.main(["classify", "--weeks", "2026-W38", "--out", str(out),
                  "--manifests-dir", str(MANIFESTS), "--snapshots-dir", str(SNAPSHOTS)])
    assert rc == 0
    lines = [json.loads(x) for x in (out / bs.RESULTS_NAME).read_text().splitlines()]
    assert len(lines) == 10
    assert {r["model_id"] for r in lines} == {"llama3.2:3b"}
    assert 0 < len(fake.calls) <= 10
    # Results never overwrite.
    assert bs.main(["classify", "--weeks", "2026-W38", "--out", str(out)]) == 2

    manifests, fixtures = _tmp_tree(tmp_path, ["2026-W38"])
    assert _graft(out / bs.RESULTS_NAME, manifests, fixtures, "--write") == 0
    assert bs.unmeasured_cells(json.loads((manifests / "2026-W38.json").read_text())) == []
