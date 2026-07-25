"""Samples that carry no measurement must never become a measurement.

The failure this guards against, observed in 2026-W27 and 2026-W29:
gpt-5.5 returned HTTP 200 with an empty body and
``finish_reason="length"`` on 7% of samples, concentrated on three
contested prompts. Because an empty string classifies as "did not
refuse", summarizes to length 0, and embeds without complaint, the
pipeline published ``refusal_rate=0.00, length.median=0, n_samples=20``
for a cell where the model had said nothing 20 times out of 20.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from meridian.analysis import usability
from meridian.corpus import load_corpus
from meridian.pipeline.manifest_writer import build_manifest
from meridian.runners.base import Sample
from meridian.storage import LocalSampleStore


def _s(text: str, *, finish: str | None = "stop", stop: str | None = None,
       idx: int = 0, prompt_id: str = "p", model_id: str = "m") -> Sample:
    return Sample(
        prompt_id=prompt_id, model_id=model_id, provider="fake",
        request_index=idx, temperature=1.0, max_tokens=1024,
        text=text, model_version_string=f"{model_id}-v",
        finish_reason=finish, stop_reason=stop,
        latency_ms=1, captured_at=datetime.now(timezone.utc),
    )


def test_truncated_empty_is_the_diagnostic_case():
    """finish_reason=length + no body identifies the token budget as
    the cause, which is what makes the fix actionable."""
    assert usability.unusable_reason(_s("", finish="length")) == "truncated-empty"
    # Anthropic spells the same thing differently.
    assert usability.unusable_reason(
        _s("", finish=None, stop="max_tokens")
    ) == "truncated-empty"


def test_empty_without_truncation_still_unusable():
    assert usability.unusable_reason(_s("", finish="stop")) == "empty"
    assert usability.unusable_reason(_s("   \n  ", finish="stop")) == "empty"


def test_truncated_but_non_empty_is_usable():
    """A response cut off mid-sentence is still a response. Only the
    empty ones are holes; excluding real-but-truncated text would
    silently bias the length distribution downward."""
    assert usability.is_usable(_s("Partial answer that got cut", finish="length"))


def test_ordinary_response_is_usable():
    assert usability.unusable_reason(_s("The Magna Carta was signed in 1215.")) is None


def test_partition_and_tally():
    samples = [
        _s("real", idx=0),
        _s("", finish="length", idx=1),
        _s("", finish="length", idx=2),
        _s("", finish="stop", idx=3),
    ]
    usable, unusable = usability.partition(samples)
    assert [s.request_index for s in usable] == [0]
    assert len(unusable) == 3
    assert usability.count_reasons(samples) == {"truncated-empty": 2, "empty": 1}
    assert usability.count_reasons([_s("fine")]) == {}


# --- pipeline integration -------------------------------------------------


def _seed_mixed(tmp_path: Path, corpus, week: str, model: str,
                n_good: int, n_empty: int) -> LocalSampleStore:
    store = LocalSampleStore(tmp_path)
    for p in corpus.public()[:2]:
        idx = 0
        for i in range(n_good):
            store.append(week, model, p.id, _s(
                f"substantive answer {i}", prompt_id=p.id, model_id=model, idx=idx,
            ))
            idx += 1
        for _ in range(n_empty):
            store.append(week, model, p.id, _s(
                "", finish="length", prompt_id=p.id, model_id=model, idx=idx,
            ))
            idx += 1
    return store


def test_unusable_samples_excluded_from_metrics(tmp_path: Path):
    corpus = load_corpus()
    store = _seed_mixed(tmp_path, corpus, "2026-W16", "m1", n_good=12, n_empty=8)
    m = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=0, bootstrap_seed=1,
    )
    assert m["metrics"]
    for rec in m["metrics"]:
        # n_samples is the basis the numbers were computed on, not the
        # number of API calls made.
        assert rec["n_samples"] == 12
        assert rec["unusable_samples"] == 8
        # The 8 empties must not drag the median to zero.
        assert rec["length"]["median"] > 0
        assert rec["flagged_for_review"] is True
        assert "no usable content" in rec["flag_reason"]


def test_fully_unusable_cell_emits_no_metric_record(tmp_path: Path):
    """The 2026-W29 sci-iq-heritability case: 20/20 empty. Publishing a
    record here would publish refusal_rate 0.00 for a model that
    answered nothing."""
    corpus = load_corpus()
    store = _seed_mixed(tmp_path, corpus, "2026-W16", "m1", n_good=0, n_empty=20)
    m = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=0, bootstrap_seed=1,
    )
    assert m["metrics"] == []
    assert len(m["unmeasured"]) == 2
    for cell in m["unmeasured"]:
        assert cell["model_id"] == "m1"
        assert cell["unusable_samples"] == 20
        assert cell["reasons"] == {"truncated-empty": 20}


def test_clean_run_reports_no_unusable(tmp_path: Path):
    """The field must stay 0/empty on healthy data, so a reader can
    trust its absence."""
    corpus = load_corpus()
    store = _seed_mixed(tmp_path, corpus, "2026-W16", "m1", n_good=20, n_empty=0)
    m = build_manifest(
        store=store, corpus=corpus, week_id="2026-W16",
        history_weeks=0, bootstrap_seed=1,
    )
    assert m["unmeasured"] == []
    for rec in m["metrics"]:
        assert rec["unusable_samples"] == 0
        assert "no usable content" not in (rec["flag_reason"] or "")
