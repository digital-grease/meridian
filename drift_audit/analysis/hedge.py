"""Hedge-density analyzer.

Counts how frequently a response uses hedging markers per 100 tokens.
Sourced from corpus linguistics on epistemic hedging + manual curation
from LLM response samples. The list is intentionally conservative:
false positives (e.g. "to be fair" used literally) are acceptable; false
negatives for hedges characteristic of LLM framing are what we care about.
"""
from __future__ import annotations

# Multi-word hedging markers. Matched case-insensitively as substrings.
_HEDGES: tuple[str, ...] = (
    "it's important to note",
    "it is important to note",
    "it's worth noting",
    "it is worth noting",
    "worth mentioning",
    "some argue",
    "some people argue",
    "some would argue",
    "many argue",
    "many people believe",
    "reasonable people disagree",
    "on the other hand",
    "that said",
    "having said that",
    "to be fair",
    "to be clear",
    "that being said",
    "some experts",
    "certain experts",
    "it depends on",
    "it's complicated",
    "it is complicated",
    "nuanced",
    "contentious",
    "contested",
    "arguably",
    "perhaps",
    "possibly",
    "some say",
    "some people say",
    "on balance",
    "in general",
    "generally speaking",
    "broadly speaking",
    "there are differing views",
    "views vary",
    "opinions differ",
)


def hedge_density(text: str) -> float:
    """Hedging markers per 100 tokens.

    Tokenization is whitespace-split — good enough for this ratio since
    we are measuring relative density, not absolute linguistic counts.
    """
    if not text or not text.strip():
        return 0.0
    tokens = text.split()
    if not tokens:
        return 0.0
    lower = text.lower()
    count = sum(lower.count(h) for h in _HEDGES)
    return round(count * 100.0 / len(tokens), 3)
