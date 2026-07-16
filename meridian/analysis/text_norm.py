"""Typographic normalisation for marker matching.

Every text analyzer here works by matching literal phrase markers
("i can't help", "it's important to note") against response text. Those
markers are written in a source file with ASCII punctuation, but
providers do not all emit ASCII punctuation, and the difference is not
cosmetic: it decides whether a marker matches at all.

Measured over the published W17-W28 snapshots, in the first 300
characters of each response:

    gpt-5.1          0 responses with ASCII '   |  337 with U+2019
    claude-opus-4-7  351 responses with ASCII ' |    0 with U+2019
    llama3.2:3b      278 responses with ASCII ' |   34 with U+2019

So an ASCII-only marker list silently encodes "match Anthropic, ignore
OpenAI". Because refusals are dense in contractions ("I can't",
"I won't", "I'm not able to"), that mis-scored gpt-5.1's refusal rate
on the refusal-boundary axis as 0.00 when it was really ~0.98, and
published that comparison against a named competitor for twelve weeks.

Normalising here rather than at ingest keeps the raw response bodies
byte-exact (they are the append-only record of what the provider
actually said, and are never rewritten). Only the matching view of the
text is folded to ASCII.
"""
from __future__ import annotations

import unicodedata

# Characters providers actually emit where a marker list writes ASCII.
# Values are the ASCII equivalent the marker lists are written with.
_PUNCT_FOLD = {
    "‘": "'",   # left single quote
    "’": "'",   # right single quote — the apostrophe in "can’t"
    "‚": "'",   # single low-9 quote
    "‛": "'",   # single high-reversed-9 quote
    "′": "'",   # prime
    "ʼ": "'",   # modifier letter apostrophe
    "“": '"',   # left double quote
    "”": '"',   # right double quote
    "„": '"',   # double low-9 quote
    "″": '"',   # double prime
    "‐": "-",   # hyphen
    "‑": "-",   # non-breaking hyphen — GPT emits this in "Wi‑Fi"
    "‒": "-",   # figure dash
    "–": "-",   # en dash
    "—": "-",   # em dash
    "−": "-",   # minus sign
    " ": " ",   # non-breaking space
    " ": " ",   # narrow no-break space
    " ": " ",   # thin space
}

_TRANSLATION = str.maketrans(_PUNCT_FOLD)


def normalize_for_matching(text: str) -> str:
    """Fold a response to the ASCII punctuation the marker lists use.

    NFKC first (collapses compatibility forms such as ligatures and
    full-width punctuation), then an explicit fold of the quote, dash
    and space characters NFKC deliberately leaves alone: NFKC does not
    touch U+2019, which is the exact character this exists to handle.

    Case is left alone; callers lowercase themselves.
    """
    if not text:
        return text
    return unicodedata.normalize("NFKC", text).translate(_TRANSLATION)
