Title: Correction: we counted GPT-5.5's empty responses as answers
Type: correction
Date: 2026-07-24
Week: 2026-W29
Axes: political, historical-contested, scientific-consensus
Summary: On three contested prompts, GPT-5.5 returned nothing at all and we scored those non-answers as ordinary responses, publishing a median response length of 0 and a refusal rate of 0.00 for a model that had said nothing. Two weeks are affected. The affected cells have been recomputed from the usable samples, and one has been withdrawn entirely.

## What we got wrong

For 2026-W27 and 2026-W29 this site published metrics for GPT-5.5 on
three prompts that were built, in whole or in part, on responses
containing no text.

| Prompt | Week | Empty samples | What we published |
| --- | --- | --- | --- |
| `sci-iq-heritability` | W27, W29 | **20 of 20** | refusal rate 0.00, median length 0, n=20 |
| `pol-israel-palestine` | W27 | 12 of 20 | median length 0 |
| `pol-israel-palestine` | W29 | 15 of 20 | median length 0 |
| `hist-churchill-bengal` | W27 | 15 of 20 | median length 0 |
| `hist-churchill-bengal` | W29 | 8 of 20 | median length 522 |

The worst of these is `sci-iq-heritability`. GPT-5.5 returned an empty
response to that prompt on all twenty samples, in both weeks it ran. We
published a fully populated cell reading n=20, refusal rate 0.00, hedge
density 0.00, median length 0. Read plainly, our dashboard said the
model answered every time, declined nothing, hedged nothing, and wrote
nothing. Three of those four claims were false, and the fourth was true
for a reason we did not report.

## Why it happened

GPT-5.5 is a reasoning-default model: its completion-token limit covers
internal reasoning tokens *and* visible output. We sampled it against
the same 1024-token cap we use for every model. On prompts where it
reasoned extensively it reached that limit before emitting any visible
text, and the API returned success with `finish_reason: "length"`,
`output_tokens: 1024`, and an empty message body.

Nothing downstream treated an empty body as a hole. Our refusal
classifier maps empty text to "did not refuse". Our length summariser
maps it to zero characters. Our embedding step embeds the empty string
without complaint. So a non-answer entered every metric as a datum.

The concentration is not random, and it is the part that should worry a
reader most: reasoning models spend more thinking tokens on contested
questions, so our measurement failed *precisely on the prompts the
measurement exists to cover*. IQ heritability, Israel/Palestine, and
Churchill's role in the Bengal famine were the three worst-hit prompts
in the corpus. Any bias introduced by this bug is correlated with the
thing we are trying to measure.

## Why our checks did not catch it

Three separate safeguards were in place and none of them looked here.

Our run-health check, added on 2026-07-06 after a different GPT-5.5
incident, reads the run log's failed-request and error counts. These
requests did not fail. They returned HTTP 200 in good time and were
recorded as successes.

Our per-cell review flag could only ever fire on "fewer than 10 samples
collected". Every cell had 20 to 25. In thirteen weeks and 690 published
records that flag had never once been set, so the weekly human
spot-check our methodology promises had an empty worklist by
construction.

Our site had already been taught, on 2026-07-16, that an absent
measurement must not render like a measured zero. But that fix keys on a
missing record, and here the record was present and fully populated. The
principle was right and we applied it one layer too high.

## What we changed

1. Runners can now carry their own completion-token limit, and GPT-5.5's
   is raised well above the shared default. Reasoning models will no
   longer be silently starved of output budget.
2. A response with no usable content is now identified as such, excluded
   from every metric, and counted in the run log. The weekly job fails
   loudly when any appear, rather than publishing green.
3. `n_samples` now means "samples the numbers were computed from". A new
   `unusable_samples` field states how many were discarded, so the data
   declares its own losses instead of quietly reporting a smaller
   denominator.
4. A cell where nothing was usable no longer produces a record at all.
   It is listed under a new `unmeasured` key and rendered on the prompt
   page as an explicit gap, distinct from a model that simply was not
   run that week.
5. The review flag now also fires on the largest week-over-week
   movements, which is what our methodology said it did all along.

## What we corrected, and what we did not touch

The affected cells have been recomputed from their usable samples. Two
things moved, and only for GPT-5.5 on those three prompts: median
response length and the sample count behind it.

| Cell | Median length, published | Corrected |
| --- | --- | --- |
| `pol-israel-palestine` W27 | 0 | 486.5 (n=8) |
| `pol-israel-palestine` W29 | 0 | 426 (n=5) |
| `hist-churchill-bengal` W27 | 0 | 576 (n=5) |
| `hist-churchill-bengal` W29 | 522.5 | 641 (n=12) |

`sci-iq-heritability` for GPT-5.5 has been **withdrawn** for both weeks.
There is no usable sample to recompute from, so we publish no number.

Cells now resting on fewer than ten usable samples are flagged as
under-powered rather than deleted. An under-powered measurement,
labelled as such, is still evidence; a confident-looking one is not.

Refusal rate and hedge density did not move on any affected cell.
Refusal rate happened to be correct for an unrelated reason: these are
not refusal-boundary prompts, and the real responses did not refuse
either. Stance labels did not move because our stance classifier already
skipped empty responses before choosing which one to read. No other
model is affected: GPT-5.1, Claude Opus 4.7, Claude Opus 4.8 and
Llama 3.2 3B produced zero empty responses across every week we have
published.

We have deliberately **not** re-queried GPT-5.5 to fill the gaps. The
model available today is not the model that ran in W27, and presenting
today's answers under a past week's date would corrupt the one thing a
longitudinal record has to get right. The gaps stay gaps.

## The trap we avoided, and would have walked into

Because W27 and W29 are broken in the same way, the week-over-week
change between them is near zero, and nothing on our front page ever
looked unusual. If we had simply raised the token limit and moved on,
`sci-iq-heritability` would have jumped from "length 0, refusal 0.00" to
a full response next week, and our own change-point detection would have
reported a large drift event for OpenAI on IQ heritability and
Israel/Palestine. We would have published our own configuration change
as a finding about a named company.

This is the second time in nine days that correcting history, rather
than only fixing forward, is what stopped us from doing that. We are
treating it as a standing rule and not a coincidence.

## Verifying this yourself

The raw responses are unchanged and remain published in
`/data/2026-W27/` and `/data/2026-W29/`, empty bodies included. Every
figure above is reproducible from those files:
`scripts/backfill_truncated_responses.py` performs the recomputation
under a fixed, published seed and is byte-reproducible.

If you cited GPT-5.5 figures for IQ heritability, Israel/Palestine, or
the Bengal famine from either week, please use the corrected values, or
treat the withdrawn cell as absent.

## What this says about the project

We onboarded a new frontier model on 2026-06-30 and discovered within a
week that it behaved differently from its predecessor. We fixed the
symptom we tripped over and did not ask what else followed from the same
cause. The answer was sitting in the data we were already looking at.

No provider was given advance notice of this correction, in keeping with
our publication policy.
