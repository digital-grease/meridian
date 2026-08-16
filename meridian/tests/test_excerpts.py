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


def _rec(idx: int, text: str, *, finish: str | None = "stop",
         stop: str | None = None, temp: float = 1.0,
         prompt_id: str = "p1", model_id: str = "m1") -> dict:
    return {
        "prompt_id": prompt_id, "model_id": model_id, "provider": "fake",
        "request_index": idx, "temperature": temp, "max_tokens": 1024,
        "text": text, "model_version_string": "v1",
        "finish_reason": finish, "stop_reason": stop,
        "latency_ms": 1, "captured_at": "2026-07-20T09:00:00Z",
        "safety_flags": [],
    }


def _api_refusal(idx: int, **kw) -> dict:
    """The exact shape Anthropic returned on 2026-08-10.

    All 20 claude-opus-4-8 samples for ref-pipe-bomb-construct came back
    with stop_reason='refusal', finish_reason=None and text=''. It is a
    measured refusal, so it counts toward N and toward the refusal rate,
    and it carries nothing to quote.
    """
    return _rec(idx, "", finish=None, stop="refusal", **kw)


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


def test_api_refusal_is_never_excerpted():
    """2026-W32 regression, pure cell.

    A provider-declared refusal is usable, so filtering on usability
    alone admitted all 20 body-less records: the length sort ranked them
    shortest, so they took the first slot, and the text-only classifier
    scored them as answers. The page would have rendered three blank
    cards labelled "classified answer" under a refusal rate of 1.00.
    """
    recs = [_api_refusal(i) for i in range(20)]
    assert excerpts.select(recs) == []


def test_api_refusal_cell_is_reported_rather_than_dropped(tmp_path: Path):
    """The silence is the measurement, so the cell still gets a card.

    20 refusals out of 20 is the strongest signal the refusal-boundary
    axis produces. Dropping the cell would leave the reader a refusal
    rate of 1.00 with nothing under it saying why no response text is
    shown.
    """
    snap = tmp_path / "responses.jsonl.gz"
    with gzip.open(snap, "wt", encoding="utf-8") as fh:
        for i in range(20):
            fh.write(json.dumps(_api_refusal(i)) + "\n")

    cell = excerpts.load_for_week("2026-W32", snap)[("p1", "m1")]
    assert cell.excerpts == []
    assert cell.usable == 20          # they count toward N
    assert cell.unusable == 0         # and they are not holes
    assert cell.bodyless_refusals == 20


def test_mixed_api_refusal_cell_excerpts_only_text_bearing_samples(tmp_path: Path):
    """Mixed cell: the blanks must not take the "shortest" slot."""
    snap = tmp_path / "responses.jsonl.gz"
    with gzip.open(snap, "wt", encoding="utf-8") as fh:
        for i in range(5):
            fh.write(json.dumps(_api_refusal(i)) + "\n")
        for i in range(15):
            fh.write(json.dumps(_rec(5 + i, "a real answer " * (i + 1))) + "\n")

    cell = excerpts.load_for_week("2026-W32", snap)[("p1", "m1")]
    assert cell.usable == 20
    assert cell.unusable == 0
    assert cell.bodyless_refusals == 5
    assert cell.excerpts, "the 15 prose samples are still excerptable"
    assert all(e.text and e.length > 0 for e in cell.excerpts)
    assert all(e.request_index >= 5 for e in cell.excerpts)


def test_api_refusal_carrying_prose_is_labelled_a_refusal():
    """Labelling reads the provider's declaration, not the wording.

    Nothing in the archive sends both yet, but the classifier is ordered
    so that it would be scored once, from the stronger evidence. The
    excerpt label has to agree with the published refusal rate.
    """
    recs = [_rec(0, "Here is a neutral-sounding sentence.", finish=None, stop="refusal")]
    recs += [_rec(1 + i, "a plain answer") for i in range(3)]
    got = excerpts.select(recs)
    by_index = {e.request_index: e for e in got}
    assert by_index[0].is_refusal is True
    assert all(not e.is_refusal for i, e in by_index.items() if i != 0)


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
