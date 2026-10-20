Title: Roster change: Qwen 3.6 joins the local models, alongside Llama
Type: notice
Date: 2026-10-20
Week: 2026-W43
Axes: refusal-boundary, political, historical-contested, scientific-consensus, neutral-control, factual-stability
Summary: Starting with the run labelled 2026-W43, a second model runs on our own hardware every week: Qwen 3.6 (27 billion parameters), next to the Llama 3.2 baseline we already run. Llama is not replaced, so the noise-floor series it provides continues unbroken. Qwen runs on its own pinned copy of the inference software, with thinking switched off.

## What changes

| Model | Role | Week |
| --- | --- | --- |
| `qwen3.6:27b` | new series, every week | first week **2026-W43** |
| `llama3.2:3b` | unchanged, every week | continues |

Weeks are run labels. A run is labelled with the ISO week that ended the
day before it starts, so 2026-W43 is the run of Monday 26 October 2026.

The commercial roster is unchanged: GPT-6 Astra on odd weeks, Claude Opus
5.5 on even weeks.

## Why add a model rather than replace Llama

The local models are the control group. Their weights never change, so
any week-to-week movement in their answers is sampling noise, and that
noise floor is what we measure the commercial models' drift against. A
noise floor is only useful if it is continuous. Replacing Llama would
start that series again from nothing, so Llama stays.

Qwen is added because Llama 3.2 is small and from 2024. A current
open-weight model gives a second, more capable fixed reference. Qwen is
also trained in China, which matters for the historical-contested axis,
where we want to compare how models from different places treat the
same events.

## Why Qwen runs on separate inference software

Our record pins each local model to the exact bytes of its weights. It
does not pin the software that runs them, and that software matters too:
a new version of the inference engine can change a model's answers even
when the weights are identical.

Qwen 3.6 needs a newer version of that software (ollama) than the one
that runs Llama. Upgrading it would risk moving the Llama baseline for
reasons that have nothing to do with drift. So we do not upgrade it.
Qwen runs on a second copy of the software, pinned to version 0.40.6,
and Llama keeps the version it has always run on.

## Things to know when reading the new series

**It is the 4-bit version.** We run the 4-bit weights (Q4_K_M) that
ollama distributes under the `qwen3.6:27b` name, not the full-precision
original, which does not fit on our GPU.

**Its thinking is switched off.** Qwen 3.6 reasons before answering by
default. Left that way, it spent our whole 1024-token response limit on
reasoning and returned no answer at all. With thinking off it answers
directly, the same way Llama does. This is the opposite of Claude Opus
5.5, whose thinking cannot be switched off, so the two are not measured
in quite the same way.

**It is sampled like Llama:** 20 answers at temperature 1 and 5 at
temperature 0 for every prompt, every week.

**Its first weeks have nothing to compare against.** Drift needs a
history, so its charts start filling in from its second week.

## A later weekly update

Qwen 3.6 is much larger than Llama 3.2, and on our hardware it answers
one request at a time, so it adds about three and a half hours to each
weekly run. It costs no API money. To leave room for the longer run, the
weekly data now goes up at 18:00 UTC on Mondays instead of 13:00 UTC.
