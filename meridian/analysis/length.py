"""Length statistics over a batch of responses.

Whitespace-tokenized word counts. A proper tokenizer (tiktoken, sentencepiece)
gives more accurate counts but adds dependencies and varies by provider;
word count is sufficient for the relative measure we care about.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class LengthSummary:
    median: float
    p25: float
    p75: float
    n: int


def _word_count(text: str) -> int:
    return len(text.split()) if text else 0


def summarize_lengths(texts: list[str]) -> LengthSummary:
    counts = [_word_count(t) for t in texts]
    n = len(counts)
    if n == 0:
        return LengthSummary(median=0.0, p25=0.0, p75=0.0, n=0)
    counts_sorted = sorted(counts)
    return LengthSummary(
        median=float(statistics.median(counts_sorted)),
        p25=_percentile(counts_sorted, 25),
        p75=_percentile(counts_sorted, 75),
        n=n,
    )


def _percentile(sorted_counts: list[int], pct: float) -> float:
    if not sorted_counts:
        return 0.0
    if len(sorted_counts) == 1:
        return float(sorted_counts[0])
    # Linear interpolation between the two nearest ranks.
    k = (len(sorted_counts) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_counts) - 1)
    if f == c:
        return float(sorted_counts[f])
    return float(sorted_counts[f] + (sorted_counts[c] - sorted_counts[f]) * (k - f))
