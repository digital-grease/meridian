Title: Correction: we understated refusal rates for GPT-5.1, GPT-5.5, and Llama 3.2
Type: correction
Date: 2026-07-16
Week: 2026-W28
Axes: refusal-boundary
Summary: A bug in our refusal classifier caused us to publish a refusal rate of roughly 0.00 for OpenAI's models on the refusal-boundary axis. The true figure is roughly 0.98. Every affected week has been recomputed from the original raw responses.

## What we got wrong

From 2026-W17 through 2026-W28, this site published a refusal rate of
0.00 to 0.01 for GPT-5.1 on the refusal-boundary axis, against 0.79 to
0.80 for Claude Opus 4.7. Read plainly, our dashboard said OpenAI's
model complied with requests for methamphetamine synthesis, pipe bomb
construction, racist jokes, and unauthorized network access, while
Anthropic's model declined them.

That was wrong, and it was wrong in the most damaging possible
direction. The corrected figures:

| Model | Published | Corrected |
| --- | --- | --- |
| GPT-5.1 (W17, W19, W21, W23, W25) | 0.00 to 0.01 | **0.94 to 0.99** |
| GPT-5.5 (W27) | 0.00 | **0.84** |
| Llama 3.2 3B (W19 through W28) | 0.29 to 0.41 | **0.57 to 0.64** |
| Claude Opus 4.7 / 4.8 | 0.79 to 0.80 | unchanged |

GPT-5.1 does not refuse fewer of these requests than Claude. It refuses
slightly more. We published the opposite for twelve weeks.

## Why it happened

Our refusal classifier works by matching a list of literal phrases
against the start of each response. Those phrases were written in a
source file with ordinary ASCII apostrophes, as in `i can't help`.

Providers do not all use the same apostrophe. Measured across the first
300 characters of every response in our published snapshots:

| Model | Responses using ASCII `'` | Responses using U+2019 `’` |
| --- | --- | --- |
| GPT-5.1 | 0 | 337 |
| Claude Opus 4.7 | 351 | 0 |
| Llama 3.2 3B | 278 | 34 |

GPT-5.1 uses the typographic apostrophe exclusively. Claude uses the
ASCII apostrophe exclusively. So `i can't help` never matched
`I can’t help with that.`, and because refusals are dense in
contractions ("I can't", "I won't", "I'm not able to"), our classifier
was in effect configured to detect Anthropic's refusals and ignore
OpenAI's.

This is not a subtle statistical artifact. It is a one-character bug
that happened to fall along provider lines, and it produced a confident,
specific, published claim about a named company that was the reverse of
the truth.

## Why our tests did not catch it

The classifier was covered by a hand-labeled golden set of more than 100
examples, with an F1 threshold enforced in CI. It passed at F1 1.0
throughout.

Every example in that golden set was typed by a person into a text
editor, so every example used ASCII apostrophes. The eval contained no
real provider output at all. It tested the classifier against our idea
of what a refusal looks like, and never against what the providers
actually sent us. A test suite that only ever sees synthetic input can
report perfect accuracy while the thing it guards is broken in
production.

## What we changed

1. Response text is now folded to ASCII punctuation before phrase
   matching. Raw response bodies are untouched and remain byte-exact:
   only the matching view of the text is normalized.
2. The golden set now pins verbatim provider responses, apostrophes and
   all, copied directly from our published snapshots.
3. New tests assert that the classifier returns the same verdict
   regardless of apostrophe style, and that the phrase lists stay
   ASCII-only.

## What we corrected, and what we did not touch

Every affected week (W17 through W28) has been recomputed from the
original raw responses, which were never wrong. The recomputation
changed only:

- `refusal_rate` and `refusal_ci`
- the refusal drift p-value, and the Benjamini-Hochberg adjusted
  p-values that share its within-week family
- change-point detection on the refusal series

All 50 corrected records moved in the same direction, upward. The bug
could only cause missed refusals, never false ones.

Hedge density, response length, stance, and embedding drift were checked
and are unaffected. In particular, GPT's markedly lower hedge density is
a real measurement and not a symptom of this bug.

One consequence worth stating plainly: the previously published series
showed GPT's refusal rate as flat at 0.00. Had we fixed the classifier
without recomputing history, our own change-point detection would have
seen 0.00 jump to 0.98 this week and reported it as a drift event. We
would have published our bug as a finding about OpenAI. Correcting the
history is what prevents that.

## Verifying this yourself

The raw responses have not changed and are published in
`/data/2026-W19/` and every other week's snapshot. The apostrophe counts
above, and every corrected figure, are reproducible from those files
against the current classifier.

## What this says about the project

We exist to hold model providers to a public, reproducible record. That
obligation runs in both directions: our own errors have to be as visible
as the drift we report, especially when the error flatters one provider
at another's expense. No provider was given advance notice of this
correction, in keeping with our publication policy.

If you cited the refusal-boundary figures for GPT-5.1 or GPT-5.5 at any
point before 2026-07-16, please use the corrected numbers above.
