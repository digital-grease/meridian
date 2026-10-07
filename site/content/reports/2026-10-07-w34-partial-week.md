Title: 2026-W34 is now published, as a partial week
Type: notice
Date: 2026-10-07
Week: 2026-W34
Axes: refusal-boundary, political, historical-contested, scientific-consensus, neutral-control, factual-stability
Summary: The run for 2026-W34 was stopped partway through by our own infrastructure, and until now we said its data would not be published. It is published today as a partial week: Claude Opus 4.8 and Llama 3.2 3B are complete, and Claude Opus 5 has 259 of its 600 samples across 13 of 30 prompts. Nothing was re-sampled. Every gap is stated in the data itself.

## What happened

The run labelled 2026-W34 started on schedule on 2026-08-24. It was the
first week in which Claude Opus 5 ran alongside Claude Opus 4.8, which
roughly doubled the time the run needed. The system that dispatches the
run applied a default execution limit of one hour that we had never
changed, and stopped the run when the hour was up. Claude Opus 4.8 and
Llama 3.2 3B had finished. Claude Opus 5 had not.

A run stopped that way writes nothing at the end: no manifest, no
snapshot of responses, no entry in our run log. The responses it had
already captured were safe on the sampling machine. They were copied to
our archive on 2026-10-05.

## What is published

| Model | Samples | Prompts complete | Status |
| --- | --- | --- | --- |
| Claude Opus 4.8 | 600 of 600 | 30 of 30 | complete |
| Llama 3.2 3B | 750 of 750 | 30 of 30 | complete |
| Claude Opus 5 | 259 of 600 | 12 of 30, one more at 19 of 20 | partial |

The 17 prompts Claude Opus 5 never reached have no measurement for this
week and no row in the data. The prompt it was part way through,
`sci-iq-heritability`, is published at the 19 samples it has.

## How it is marked

* The week's manifest, `data/manifests/2026-W34.json`, carries
  `partial: true`, a per-model `coverage` list naming every missing and
  cut-short prompt, and `notes` explaining when and how it was built.
* Our run log has one entry for the week, marked `recovery: true`. It was
  written on 2026-10-07 from the archived responses, not by the run, and
  its note says so. Its pair counts are what the archive holds: 72 of 90
  pairs complete and 18 not.
* The [coverage page](/data/coverage/) shows the week as partial for
  Claude Opus 5, and the [methodology](/methodology/#data-gaps) records
  the gap.

## Why publish it at all

Two reasons. First, the responses are real measurements, captured on
the Monday the run was scheduled for, exactly like every other week, and
nothing has been added to them since. Second, this week was already
part of the record. Its metrics had been carried in the history of every
later manifest and shown under [`/data/2026-W34/`](/data/2026-W34/)
since 2026-W35, and two later weeks measured change against it:
Llama 3.2 3B in 2026-W35, and three Claude cells in 2026-W36. Our
methodology page said the week would not be published, which was not
consistent with what the site was already doing. Publishing the week
with its coverage stated makes the record match what it was using.

## What we did not do

We did not re-sample anything. Asking Claude Opus 5 today would measure
the model as it is today and label it as August, which is precisely the
substitution this project exists to detect. The missing prompts stay
missing.

## One small difference to expect

The manifest for 2026-W34 was built from the archived responses with the
same code a live run uses, and with a fixed random seed so that anyone
can rebuild it exactly. Confidence intervals and test statistics are
drawn by resampling, and the copies of 2026-W34 already embedded in
later weeks' history were drawn without a fixed seed. Some intervals and
p-values may therefore differ in the last decimal places between those
copies and this manifest. Every count, rate and length is the same
computation on the same responses. Stance had never been classified for
this week, because the run was stopped before that step; it was
classified on 2026-10-07, when the manifest was built, with the same
pinned classifier model every week uses.

## The cause is fixed

The execution limit is now six hours, and a scheduled check stops the
sampling machine if a run ever ends without stopping it, which this one
also failed to do.

As with everything we publish, no provider saw this before publication.
