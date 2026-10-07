Title: Roster change: Claude Opus 5.5 and GPT-6 Astra replace three models in October
Type: notice
Date: 2026-10-06
Week: 2026-W41
Axes: refusal-boundary, political, historical-contested, scientific-consensus, neutral-control, factual-stability
Summary: Starting with the run labelled 2026-W41, GPT-6 Astra replaces GPT-5.5 on odd weeks, and starting with 2026-W42 Claude Opus 5.5 replaces both Claude Opus 4.8 and Claude Opus 5 on even weeks. Each old and new model run side by side for exactly one week so the handover can be compared directly. The three older series then end. Their data stays published.

## What changes

The commercial models measured here change in October. The new models
are each provider's current flagship, and the models they replace are no
longer the ones most people get by default.

| Model | Role | Week |
| --- | --- | --- |
| `gpt-6-astra` | new series, odd weeks | first week **2026-W41** |
| `gpt-5.5` | series ends | last week **2026-W41** |
| `claude-opus-5-5` | new series, even weeks | first week **2026-W42** |
| `claude-opus-4-8` | series ends | last week **2026-W42** |
| `claude-opus-5` | series ends | last week **2026-W42** |

Weeks are run labels. A run is labelled with the ISO week that ended the
day before it starts, so 2026-W41 is the run of Monday 12 October 2026
and 2026-W42 the run of Monday 19 October 2026.

From 2026-W43 on, the commercial roster is GPT-6 Astra on odd weeks and
Claude Opus 5.5 on even weeks. The local Llama baseline still runs every
week, and the stance classifier (Claude Haiku 4.5) is unchanged.

## One week of overlap

A new model is not a continuation of the old one. A drift record whose
subject silently changes is not a drift record, so each new model starts
its own series rather than inheriting the old model's history.

To make the handover readable anyway, each retiring model runs in the
same week as its successor, once:

- **2026-W41:** GPT-5.5 and GPT-6 Astra answer the same corpus in the
  same week.
- **2026-W42:** Claude Opus 4.8, Claude Opus 5 and Claude Opus 5.5 answer
  the same corpus in the same week.

Same week means same prompts and the same state of the world, so
differences between the old and new model in that week are differences
between the models, not between weeks. That is the comparison to use
when asking "did the switch change the picture?" Comparing the new
model's later weeks with the old model's earlier weeks mixes the change
of model with ordinary drift.

The retired series stay on the site. Their model pages, weekly snapshots
and raw data remain where they are and keep their links. They simply stop
gaining new weeks.

## Things to know when reading the new series

**Claude Opus 5.5 thinks less by default than Claude Opus 5 did.** We
measure every model at its provider defaults and do not set a reasoning
effort level. Anthropic's default effort for Claude Opus 5.5 is "medium",
where Claude Opus 5's default was "high". Part of any difference between
the two in the 2026-W42 overlap may therefore come from that default,
and not only from the new model. We record it rather than correct for
it, because the default is what people using the model get.

**Claude Opus 5.5 always thinks.** Its internal reasoning cannot be
switched off, and it does not accept a temperature setting. As with the
other thinking-by-default models (see the
[methodology page](/methodology/#thinking-models)), it is sampled 20
times per prompt at the default setting, with no temperature-0 samples.

**GPT-6 Astra is treated like GPT-5.5.** OpenAI has not documented which
request settings it accepts. We send it exactly what we sent GPT-5.5: no
temperature setting (so, again, 20 samples per prompt and no
temperature-0 samples) and the same 8192-token response limit, which
leaves room for its reasoning so that it is not cut off before it
answers.

**No substitute model ever answers.** Some providers can be asked to
hand a request to a different model when the first one declines it.
That option is never enabled here. Every response stored under a
model's name came from that model, and a refusal is recorded as a
refusal, not replaced by another model's answer.

## Cost

The overlap weeks cost more because they run two or three frontier
models at once. Our pre-run estimates are $61.11 for 2026-W41 and $42.73
for 2026-W42, against $38.20 for an ordinary odd week (GPT-6 Astra) and
$15.28 for an ordinary even week (Claude Opus 5.5) from 2026-W43 on.
The run's spending ceiling now scales with each week's estimate instead
of being a single fixed number, with a hard maximum, so the overlap
weeks can run without lifting the cap on ordinary ones. Details are in
`meridian/BUDGET.md`.
