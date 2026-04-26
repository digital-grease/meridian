"""Stance scoring across all (prompt × model) pairs in a week.

Walks the local sample store for the given week, picks one
representative response per (prompt × model) pair, and runs each
through the stance classifier. The result is a
``dict[(prompt_id, model_id), StanceResult]`` ready to pass to
``manifest_writer.build_manifest``.

Representative-response selection
---------------------------------

The classifier runs once per pair, not once per sample. To pick the
single response we ask the classifier about, we use the longest
non-refusal sample (longest by character count). Rationale:

  * Refusals carry no stance signal and would just get classified "na".
  * Among substantive responses, longer responses generally carry more
    stance content; the median sample is fine but the longest is
    informative for the rare model that generates a thin neutral
    answer most of the time and a substantive opinion occasionally.

The classifier itself is cached on (prompt_id, response_hash) inside
:class:`LLMStanceClassifier`, so re-running the pipeline against the
same samples is free.

Cost
----

For the v1 corpus (30 prompts, ~12 stance-bearing across the political
+ historical-contested axes, ~3 models per week), expected cost is
``12 × 3 ≈ 36 Haiku calls/week`` at <$0.01 total. Negligible against
the ~$30/week sampling spend.
"""
from __future__ import annotations

import logging

from meridian.analysis.refusal import classify_refusal
from meridian.analysis.stance import (
    STANCE_AXES,
    StanceClassifier,
    StanceResult,
)
from meridian.corpus import Corpus, Prompt
from meridian.runners.base import Sample
from meridian.storage import LocalSampleStore

_log = logging.getLogger(__name__)


def _representative_response(samples: list[Sample]) -> str | None:
    """Pick one response from a pair's samples to classify on.

    Drops refusals; among substantive responses, picks the longest by
    character count. Returns ``None`` if every sample is a refusal or
    the list is empty (the classifier should treat that pair as ``na``).
    """
    substantive: list[str] = []
    for s in samples:
        text = s.text or ""
        if not text.strip():
            continue
        if classify_refusal(text).is_refusal:
            continue
        substantive.append(text)
    if not substantive:
        return None
    return max(substantive, key=len)


async def collect_stance_results(
    *,
    classifier: StanceClassifier,
    store: LocalSampleStore,
    corpus: Corpus,
    week_id: str,
) -> dict[tuple[str, str], StanceResult]:
    """Run stance classification across the week's stored samples.

    Only stance-bearing axes (`STANCE_AXES`) are classified. Pairs on
    other axes get an explicit ``na`` so manifest_writer renders them
    consistently.
    """
    out: dict[tuple[str, str], StanceResult] = {}
    prompts_by_id: dict[str, Prompt] = {p.id: p for p in corpus.all()}

    for model_id in store.models_for_week(week_id):
        for prompt_id in store.prompts_for(week_id, model_id):
            prompt = prompts_by_id.get(prompt_id)
            if prompt is None:
                continue
            if prompt.axis not in STANCE_AXES:
                # Don't even invoke the classifier; skipping the cache
                # too keeps the cache small and audit-readable.
                out[(prompt_id, model_id)] = StanceResult(
                    stance="na",
                    confidence=1.0,
                    reason="axis-excluded",
                )
                continue
            samples = store.read(week_id, model_id, prompt_id)
            response = _representative_response(samples)
            if response is None:
                out[(prompt_id, model_id)] = StanceResult(
                    stance="na",
                    confidence=1.0,
                    reason="no-substantive-response",
                )
                continue
            try:
                result = await classifier.classify(
                    prompt_id=prompt_id,
                    axis=prompt.axis,
                    prompt_text=prompt.text,
                    response_text=response,
                )
            except Exception as e:  # pragma: no cover - real-API path
                _log.warning(
                    "stance classification failed for %s / %s: %s",
                    model_id, prompt_id, e,
                )
                result = StanceResult(
                    stance="na", confidence=0.0,
                    reason=f"classifier-error: {type(e).__name__}",
                )
            out[(prompt_id, model_id)] = result
    return out
