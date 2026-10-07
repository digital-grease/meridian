"""The public coverage ledger: /data/coverage/ and /methodology/#data-gaps.

Until 2026-10 the record's gaps were stated in hand-written prose that
had drifted from the data both ways: 2026-W34 was called unpublished
while its metrics were served under /data/2026-W34/, and 2026-W36 to
W38 (Anthropic credit exhausted, the stance classifier down for every
model) were not mentioned at all. These tests pin the replacement: a
per-(week, model) table derived from the manifest, the run log and the
gap ledger, and a #data-gaps list generated from the ledger.

Every input is a tmp_path file passed through the MERIDIAN_* overrides.
The one test that reads the real ``data/gaps.jsonl`` only reads it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "site" / "src"))

import data_coverage as cov_mod  # noqa: E402
from schema import Manifest  # noqa: E402

_CP = {"refusal_rate": [], "hedge_density": [], "length_median": []}
_PROMPTS = ("pol-a", "pol-b", "fact-c")


def _metric(prompt_id: str, model_id: str, n: int = 20, **kw) -> dict:
    rec = {
        "prompt_id": prompt_id, "model_id": model_id, "n_samples": n,
        "refusal_rate": 0.0, "refusal_ci": {"lower": 0.0, "upper": 0.0},
        "hedge_density": 1.0,
        "length": {"median": 100.0, "p25": 80.0, "p75": 120.0, "n": n},
        "stance": "na", "stance_confidence": None,
        "embedding_centroid_shift": None,
        "refusal_drift": None, "hedge_drift": None, "length_drift": None,
        "change_points": _CP, "flagged_for_review": False, "flag_reason": None,
    }
    rec.update(kw)
    return rec


def _week(model_prompts: dict[str, tuple[str, ...]], n: dict[str, int] | None = None) -> list[dict]:
    n = n or {}
    return [
        _metric(p, model, n.get(model, 20))
        for model, prompts in model_prompts.items() for p in prompts
    ]


def _manifest_dict() -> dict:
    """2026-W28 to 2026-W33 with W30 and W31 lost.

    ``ctrl`` (local) runs every week, ``even`` on even weeks, ``odd`` on
    odd weeks. In W32 ``even`` covers one prompt of three (partial).
    """
    weeks = {
        "2026-W28": {"ctrl": _PROMPTS, "even": _PROMPTS},
        "2026-W29": {"ctrl": _PROMPTS, "odd": _PROMPTS},
        "2026-W32": {"ctrl": _PROMPTS, "even": ("pol-a",)},
    }
    return {
        "schema_version": 2,
        "snapshot": {
            "week_id": "2026-W33",
            "generated_at": "2026-08-17T00:00:00+00:00",
            "corpus_git_sha": "abc1234", "pipeline_version": "0.1.0",
        },
        "models": [
            {"model_id": "ctrl", "display_name": "ctrl", "provider": "local",
             "version_string": "v", "available": True},
            {"model_id": "even", "display_name": "even", "provider": "acme",
             "version_string": "v", "available": False},
            {"model_id": "odd", "display_name": "odd", "provider": "zeta",
             "version_string": "v", "available": True},
        ],
        "prompts": [
            {"prompt_id": "pol-a", "axis": "political", "title": "A",
             "text_hash": "a" * 64, "held_out": False},
            {"prompt_id": "pol-b", "axis": "political", "title": "B",
             "text_hash": "b" * 64, "held_out": False},
            {"prompt_id": "fact-c", "axis": "factual-stability", "title": "C",
             "text_hash": "c" * 64, "held_out": False},
        ],
        "metrics": _week({"ctrl": _PROMPTS, "odd": _PROMPTS}),
        "history": [
            {"week_id": w, "generated_at": "2026-07-01T00:00:00+00:00",
             "metrics": _week(mp)}
            for w, mp in weeks.items()
        ],
        "flagged": [],
        "silent_update_warnings": [],
    }


def _row(week: str, per_runner: dict[str, int], **kw) -> dict:
    row = {
        "week_id": week, "started_at": "x", "finished_at": "x", "host": "h",
        "pid": 1, "config_hash": "c", "runners": [], "total_samples_written": 0,
        "pairs_complete": 0, "pairs_skipped": 0, "pairs_failed": 0,
        "per_runner_samples": per_runner, "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
    }
    row.update(kw)
    return row


_RUN_LOG = [
    _row("2026-W28", {"local/ctrl": 60, "acme/even": 60}),
    _row("2026-W29", {"local/ctrl": 60, "zeta/odd": 60}),
    _row("2026-W32", {"local/ctrl": 60, "acme/even": 20}),
    _row("2026-W33", {"local/ctrl": 60, "zeta/odd": 60}),
]

#: A reconstructed entry, as the W34 recovery appends. Kept out of the
#: site-build fixture: the internal health page reads the log through
#: ``RunLogEntry``, which has to learn the field first. The coverage
#: reader takes raw JSON and must not care.
_RECOVERY_ROW = _row(
    "2026-W33", {"local/ctrl": 0, "zeta/odd": 0}, recovery=True,
    note="reconstructed from archived raw samples",
)

_LEDGER = [
    {"week_id": "2026-W30", "scope": "all", "kind": "lost",
     "runners": ["local/ctrl", "acme/even"], "reason": "capacity W30",
     "evidence": ["issue #24", "commit f352103"], "recorded_at": "2026-10-07"},
    # No kind: the health check has always read a line as a lost week.
    {"week_id": "2026-W31", "reason": "capacity W31",
     "runners": ["local/ctrl", "zeta/odd"]},
    {"week_id": "2026-W29", "scope": "zeta/odd", "kind": "degraded",
     "reason": "truncated responses",
     "evidence": ["/reports/2026-07-24-truncated-response-correction/"]},
    {"week_id": "2026-W33", "scope": "stance", "kind": "lost",
     "reason": "classifier balance empty", "evidence": ["issue #38"]},
    # Outside the build's window: ignored, not rendered.
    {"week_id": "2026-W10", "scope": "all", "kind": "lost", "reason": "too early"},
]


def _write_inputs(tmp_path: Path, ledger_extra: str = "") -> dict[str, Path]:
    run_log = tmp_path / "run_log.jsonl"
    run_log.write_text("".join(json.dumps(r) + "\n" for r in _RUN_LOG))
    gaps = tmp_path / "gaps.jsonl"
    gaps.write_text("".join(json.dumps(r) + "\n" for r in _LEDGER) + ledger_extra)
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    # W33's committed manifest: the classifier failed on odd's two
    # stance-bearing cells and scored ctrl's.
    w33 = _manifest_dict()
    for m in w33["metrics"]:
        if m["prompt_id"].startswith("pol-"):
            if m["model_id"] == "odd":
                m.update(stance="na", stance_confidence=0.0)
            else:
                m.update(stance="neutral", stance_confidence=0.85)
        else:
            m.update(stance="na", stance_confidence=1.0)
    (manifests / "2026-W33.json").write_text(json.dumps(w33))
    return {"run_log": run_log, "gaps": gaps, "manifests": manifests}


def _coverage(tmp_path: Path, ledger_extra: str = "") -> cov_mod.Coverage:
    paths = _write_inputs(tmp_path, ledger_extra)
    raw, skipped = cov_mod.load_gap_ledger(paths["gaps"])
    return cov_mod.build_coverage(
        Manifest.model_validate(_manifest_dict()),
        run_log_rows=cov_mod.load_run_log_rows(paths["run_log"]),
        ledger_raw=raw, ledger_skipped=skipped,
        manifests_dir=paths["manifests"],
    )


def _cell(cov: cov_mod.Coverage, week: str, model: str) -> cov_mod.CellCoverage:
    w = cov.week(week)
    assert w is not None
    return next(c for c in w.cells if c.model_id == model)


# ---------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------


def test_every_calendar_week_is_a_row_newest_first(tmp_path: Path):
    cov = _coverage(tmp_path)
    assert [w.week_id for w in cov.weeks] == [
        "2026-W33", "2026-W32", "2026-W31", "2026-W30", "2026-W29", "2026-W28",
    ]
    # Every row has every column, so the table is a true matrix.
    assert all(len(w.cells) == 3 for w in cov.weeks)
    # First week seen, then provider/model key.
    assert [m for _, m in cov.models] == ["even", "ctrl", "odd"]


def test_lost_week_uses_the_ledger_roster_and_reason(tmp_path: Path):
    cov = _coverage(tmp_path)
    for model in ("ctrl", "even"):
        c = _cell(cov, "2026-W30", model)
        assert c.status == "lost"
        assert c.reasons == ["capacity W30"]
        assert not c.unexplained
        assert c.samples_logged is None  # no run log entry at all
    assert _cell(cov, "2026-W30", "odd").status == "not-scheduled"
    assert [e["href"] for e in _cell(cov, "2026-W30", "ctrl").evidence] == [
        "https://github.com/digital-grease/meridian/issues/24",
        "https://github.com/digital-grease/meridian/commit/f352103",
    ]


def test_a_ledger_line_with_no_kind_reads_as_lost(tmp_path: Path):
    cov = _coverage(tmp_path)
    assert _cell(cov, "2026-W31", "odd").status == "lost"
    assert _cell(cov, "2026-W31", "even").status == "not-scheduled"


def test_off_cadence_model_is_not_scheduled_not_lost(tmp_path: Path):
    cov = _coverage(tmp_path)
    assert _cell(cov, "2026-W28", "odd").status == "not-scheduled"
    assert _cell(cov, "2026-W29", "even").status == "not-scheduled"


def test_missing_prompts_without_a_ledger_entry_are_partial_and_unexplained(tmp_path: Path):
    c = _cell(_coverage(tmp_path), "2026-W32", "even")
    assert c.status == "partial"
    assert c.unexplained
    assert (c.prompts_measured, c.prompts_total) == (1, 3)
    assert (c.samples_published, c.samples_expected) == (20, 60)


def test_ledger_kind_overrides_the_computed_status(tmp_path: Path):
    c = _cell(_coverage(tmp_path), "2026-W29", "odd")
    assert c.status == "degraded"
    assert c.evidence == [{
        "text": "/reports/2026-07-24-truncated-response-correction/",
        "href": "/reports/2026-07-24-truncated-response-correction/",
    }]


def test_a_due_model_with_zero_samples_and_no_ledger_is_lost_and_unexplained(tmp_path: Path):
    """2026-W38's shape: the run log seeds the runner at 0."""
    paths = _write_inputs(tmp_path)
    rows = cov_mod.load_run_log_rows(paths["run_log"])
    rows.append(_row("2026-W32", {"zeta/odd": 0}))
    cov = cov_mod.build_coverage(
        Manifest.model_validate(_manifest_dict()),
        run_log_rows=rows, ledger_raw=[],
    )
    c = _cell(cov, "2026-W32", "odd")
    assert c.status == "lost"
    assert c.unexplained
    assert c.samples_logged == 0


def test_logged_but_unpublished_samples_are_not_yet_published(tmp_path: Path):
    rows = [_row("2026-W32", {"zeta/odd": 60})]
    cov = cov_mod.build_coverage(
        Manifest.model_validate(_manifest_dict()), run_log_rows=rows, ledger_raw=[],
    )
    assert _cell(cov, "2026-W32", "odd").status == "scheduled"


def test_stance_failures_are_counted_from_the_committed_manifest(tmp_path: Path):
    cov = _coverage(tmp_path)
    odd = _cell(cov, "2026-W33", "odd")
    assert (odd.stance_cells, odd.stance_unscored) == (2, 2)
    ctrl = _cell(cov, "2026-W33", "ctrl")
    assert (ctrl.stance_cells, ctrl.stance_unscored) == (2, 0)
    w33 = cov.week("2026-W33")
    assert [n.scope for n in w33.notes] == ["stance"]
    assert w33.has_warnings
    assert w33.recovery_notes == []


def test_a_reconstructed_run_log_entry_is_read_and_surfaced(tmp_path: Path):
    paths = _write_inputs(tmp_path)
    rows = cov_mod.load_run_log_rows(paths["run_log"]) + [_RECOVERY_ROW]
    cov = cov_mod.build_coverage(
        Manifest.model_validate(_manifest_dict()), run_log_rows=rows, ledger_raw=[],
    )
    assert cov.week("2026-W33").recovery_notes == [
        "reconstructed from archived raw samples",
    ]
    # Its zero counts add nothing; the week's status is unchanged.
    assert _cell(cov, "2026-W33", "odd").status == "ok"


def test_ledger_outside_the_window_and_malformed_lines_are_dropped(tmp_path: Path):
    cov = _coverage(tmp_path, ledger_extra="not json\n{\"week_id\": \"W34\"}\n")
    assert "2026-W10" not in {r.week_id for r in cov.ledger}
    assert cov.ledger_skipped == [6, 7]


def test_rows_and_csv_skip_off_cadence_cells(tmp_path: Path):
    cov = _coverage(tmp_path)
    rows = cov.rows()
    assert all(r["status"] != "not-scheduled" for r in rows)
    assert len(rows) == 12
    csv_text = cov_mod.coverage_csv(cov)
    assert csv_text.splitlines()[0].startswith("week_id,provider,model_id,status")
    assert len(csv_text.splitlines()) == 13
    assert len(cov_mod.coverage_jsonl(cov).splitlines()) == 12


# ---------------------------------------------------------------------
# Rendered site
# ---------------------------------------------------------------------


def _build(tmp_path: Path) -> Path:
    paths = _write_inputs(tmp_path)
    manifest_path = tmp_path / "manifest-2026-W33.json"
    manifest_path.write_text(json.dumps(_manifest_dict()))
    dist = tmp_path / "dist"
    env = dict(
        os.environ,
        MERIDIAN_RUN_LOG=str(paths["run_log"]),
        MERIDIAN_GAPS=str(paths["gaps"]),
        MERIDIAN_MANIFESTS_DIR=str(paths["manifests"]),
    )
    result = subprocess.run(
        ["uv", "run", "python", str(REPO_ROOT / "site" / "src" / "build.py"),
         "--manifest", str(manifest_path), "--out", str(dist)],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env,
    )
    assert result.returncode == 0, f"site build failed: {result.stderr}"
    return dist


def test_site_renders_the_coverage_page_and_ledger(tmp_path: Path):
    dist = _build(tmp_path)
    cov_dir = dist / "data" / "coverage"
    page = (cov_dir / "index.html").read_text()
    urls = set((dist / "urls.txt").read_text().split())
    assert "/data/coverage/" in urls

    # One anchor per week, statuses spelled out, reasons and evidence.
    for week in ("2026-W28", "2026-W30", "2026-W31", "2026-W33"):
        assert f'id="{week}"' in page
    assert "capacity W30" in page
    assert "https://github.com/digital-grease/meridian/issues/24" in page
    assert "unexplained" in page  # W32 even, partial with no entry
    assert "Stance unmeasured on 2 of 2" in page

    # The ledger is published verbatim, with checksums over every file.
    assert (cov_dir / "gaps.jsonl").read_text() == (tmp_path / "gaps.jsonl").read_text()
    sums = dict(
        reversed(line.split("  ")) for line in (cov_dir / "SHA256SUMS").read_text().splitlines()
    )
    for name in ("coverage.csv", "coverage.jsonl", "gaps.jsonl"):
        digest = hashlib.sha256((cov_dir / name).read_bytes()).hexdigest()
        assert sums[name] == digest


def test_methodology_data_gaps_is_generated_from_the_ledger(tmp_path: Path):
    dist = _build(tmp_path)
    page = (dist / "methodology" / "index.html").read_text()
    # The anchor other pages link to survives.
    assert 'id="data-gaps"' in page
    gaps = page[page.index('id="data-gaps"'):]
    assert "capacity W30" in gaps and "capacity W31" in gaps
    assert "classifier balance empty" in gaps
    assert "too early" not in gaps
    assert 'href="/data/coverage/#2026-W30"' in gaps
    # W34 is not described as unpublished any more.
    assert "will not be published" not in page
    assert "published as a partial week" in page


def test_gap_week_and_data_pages_link_to_coverage(tmp_path: Path):
    dist = _build(tmp_path)
    gap_page = (dist / "data" / "2026-W30" / "index.html").read_text()
    assert 'href="/data/coverage/#2026-W30"' in gap_page
    assert "/data/coverage/#2026-W30" in (dist / "data" / "2026-W30" / "NO-DATA.md").read_text()
    assert 'href="/data/coverage/"' in (dist / "data" / "index.html").read_text()
    # A week with a partial model says so on its own data page.
    w32 = (dist / "data" / "2026-W32" / "index.html").read_text()
    assert "Coverage this week" in w32
    assert 'href="/data/coverage/#2026-W32"' in w32
    # A clean week does not.
    assert "Coverage this week" not in (dist / "data" / "2026-W28" / "index.html").read_text()


def test_schema_page_explains_stance_confidence_codes(tmp_path: Path):
    page = (_build(tmp_path) / "data" / "schema" / "index.html").read_text()
    assert "Empty where stance is n/a" not in page
    assert "<code>0.0</code> with stance n/a" in page
    assert "stance_reason" in page


# ---------------------------------------------------------------------
# The committed ledger (read only)
# ---------------------------------------------------------------------


def _health_module():
    spec = importlib.util.spec_from_file_location(
        "check_run_health_for_ledger", REPO_ROOT / "scripts" / "check_run_health.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_committed_gap_ledger_is_well_formed_for_both_readers():
    path = REPO_ROOT / "data" / "gaps.jsonl"
    if not path.exists():
        return
    ledger, malformed = _health_module().load_gap_ledger(str(path))
    assert malformed == []
    raw, skipped = cov_mod.load_gap_ledger(path)
    assert skipped == []
    assert sum(len(v) for v in ledger.values()) == len(raw)
    for rec in raw:
        assert rec.get("kind") in ("lost", "partial", "degraded", "note"), rec
        assert rec.get("reason"), rec
        assert rec.get("recorded_at"), rec
        assert isinstance(rec.get("evidence"), list) and rec["evidence"], rec
        scope = rec.get("scope")
        assert scope in ("all", "stance") or "/" in scope, rec
        if scope == "all" and rec["kind"] == "lost":
            assert rec.get("runners"), f"a lost week needs its roster: {rec}"
        assert "\u2014" not in json.dumps(rec, ensure_ascii=False)
