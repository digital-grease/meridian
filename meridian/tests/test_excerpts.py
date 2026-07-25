"""Sample-response drill-downs must be mechanical, not editorial.

CLAUDE.md lists cherry-picking as the first accusation this project
expects, and showing hand-chosen model responses is the easiest possible
way to earn it. The selection rule is therefore fixed and stated on the
page; these tests pin the rule, the honesty guarantees around it, and
the held-out boundary.
"""
from __future__ import annotations

import gzip
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "site" / "src"))

import excerpts  # noqa: E402

_REFUSAL = "I can't help with that request."


def _rec(idx: int, text: str, *, finish: str = "stop", temp: float = 1.0,
         prompt_id: str = "p1", model_id: str = "m1") -> dict:
    return {
        "prompt_id": prompt_id, "model_id": model_id, "provider": "fake",
        "request_index": idx, "temperature": temp, "max_tokens": 1024,
        "text": text, "model_version_string": "v1",
        "finish_reason": finish, "stop_reason": None,
        "latency_ms": 1, "captured_at": "2026-07-20T09:00:00Z",
        "safety_flags": [],
    }


def test_picks_shortest_median_longest():
    recs = [_rec(i, "x" * (10 * (i + 1))) for i in range(5)]
    got = excerpts.select(recs)
    assert [e.role for e in got] == ["shortest", "median", "longest"]
    assert [e.length for e in got] == [10, 30, 50]


def test_shows_everything_when_fewer_than_three():
    recs = [_rec(0, "aa"), _rec(1, "bbbb")]
    got = excerpts.select(recs)
    assert len(got) == 2
    assert all(e.role == "sample" for e in got)


def test_unusable_samples_are_never_excerpted():
    """An empty completion has no content to show, and showing it as a
    response would repeat the exact error this pipeline just corrected."""
    recs = [_rec(0, "a real answer"), _rec(1, "", finish="length")]
    got = excerpts.select(recs)
    assert len(got) == 1
    assert got[0].text == "a real answer"


def test_all_unusable_yields_nothing():
    recs = [_rec(i, "", finish="length") for i in range(20)]
    assert excerpts.select(recs) == []


def test_refusal_mix_is_always_represented():
    """A cell at refusal rate 0.5 whose three length-picked excerpts all
    happened to refuse would contradict the number printed above it."""
    # Refusals are the three longest, so length selection alone would
    # surface only refusals.
    recs = [_rec(i, "short answer") for i in range(3)]
    recs += [_rec(3 + i, _REFUSAL + " padding" * (i + 5)) for i in range(3)]
    got = excerpts.select(recs)
    assert any(e.is_refusal for e in got)
    assert any(not e.is_refusal for e in got)


def test_uniform_cell_gets_no_spurious_extra():
    recs = [_rec(i, f"answer number {i}") for i in range(6)]
    got = excerpts.select(recs)
    assert len(got) == excerpts.EXCERPTS_PER_CELL
    assert all(not e.is_refusal for e in got)


def test_long_response_truncated_visibly():
    recs = [_rec(0, "word " * 2000), _rec(1, "b"), _rec(2, "cc"), _rec(3, "ddd")]
    got = excerpts.select(recs)
    longest = next(e for e in got if e.role == "longest")
    assert longest.truncated is True
    assert len(longest.text) <= excerpts.MAX_EXCERPT_CHARS
    # The true length is still reported, so truncation is never silent.
    assert longest.length > excerpts.MAX_EXCERPT_CHARS


def test_selection_is_deterministic():
    recs = [_rec(i, "x" * (10 * (i + 1))) for i in range(7)]
    assert [e.request_index for e in excerpts.select(recs)] == \
           [e.request_index for e in excerpts.select(list(reversed(recs)))]


def test_held_out_prompts_never_loaded(tmp_path: Path):
    """The snapshot is the one input on the prompt-page path that could
    carry held-out prompts."""
    snap = tmp_path / "responses.jsonl.gz"
    with gzip.open(snap, "wt", encoding="utf-8") as fh:
        for i in range(3):
            fh.write(json.dumps(_rec(i, "public answer", prompt_id="pub")) + "\n")
            fh.write(json.dumps(_rec(i, "SECRET", prompt_id="heldout")) + "\n")

    got = excerpts.load_for_week("2026-W30", snap, prompt_ids={"pub"})
    assert set(got) == {("pub", "m1")}
    assert "SECRET" not in json.dumps(
        [e.text for c in got.values() for e in c.excerpts]
    )


def test_missing_snapshot_is_not_an_error(tmp_path: Path):
    assert excerpts.load_for_week("2026-W30", tmp_path / "nope.jsonl.gz") == {}


def test_counts_reported_alongside_excerpts(tmp_path: Path):
    snap = tmp_path / "responses.jsonl.gz"
    with gzip.open(snap, "wt", encoding="utf-8") as fh:
        for i in range(4):
            fh.write(json.dumps(_rec(i, f"answer {i}" * (i + 1))) + "\n")
        for i in range(2):
            fh.write(json.dumps(_rec(90 + i, "", finish="length")) + "\n")
    cell = excerpts.load_for_week("2026-W30", snap)[("p1", "m1")]
    assert cell.usable == 4
    assert cell.unusable == 2


# --- rendered page --------------------------------------------------------


def test_prompt_page_renders_samples_and_states_the_rule(tmp_path: Path):
    """The rule has to be visible to the reader; a mechanical selection
    nobody can see is indistinguishable from a hand-picked one."""
    week = "2026-W29"
    dist = tmp_path / "dist"
    result = subprocess.run(
        [
            "uv", "run", "python", str(REPO_ROOT / "site" / "src" / "build.py"),
            "--manifest", str(REPO_ROOT / "site" / "fixtures" / f"manifest-{week}.json"),
            "--out", str(dist),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr

    html = (dist / "prompts" / "pol-israel-palestine" / "index.html").read_text()
    flat = re.sub(r"\s+", " ", html)
    assert "Sample responses" in flat
    assert "mechanical, not editorial" in flat
    assert "shortest" in flat and "median-length" in flat and "longest" in flat
    # Receipts must be in the HTML itself, not fetched by JS.
    assert "<blockquote class=\"sample-text\">" in html
    # And must point at the full record.
    assert f'href="/data/{week}/"' in html


def test_withdrawn_cell_shows_no_excerpts(tmp_path: Path):
    """gpt-5.5/sci-iq-heritability had all 20 samples come back empty in
    W29; it must appear in the unmeasured notice and contribute no
    sample text."""
    dist = tmp_path / "dist"
    subprocess.run(
        [
            "uv", "run", "python", str(REPO_ROOT / "site" / "src" / "build.py"),
            "--manifest", str(REPO_ROOT / "site" / "fixtures" / "manifest-2026-W29.json"),
            "--out", str(dist),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT, check=True,
    )
    html = (dist / "prompts" / "sci-iq-heritability" / "index.html").read_text()
    assert 'id="samples-gpt-5.5"' not in html
    assert "Not measurable this week" in html
