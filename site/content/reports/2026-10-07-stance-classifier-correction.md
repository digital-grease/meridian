Title: Correction: three weeks of stance results we published as "n/a" were never measured
Type: correction
Date: 2026-10-07
Week: 2026-W38
Axes: political, historical-contested
Summary: For 2026-W36, W37 and W38 every stance result we published for every model was a failed call to our stance classifier, recorded as "n/a" in a form that read like a finding. The responses were captured in full, so we have now classified them, exactly as we would have at the time. Only the stance fields changed.

## What we got wrong

Meridian scores the stance of each model's answer on the political and
historical-contested prompts as pro, anti, neutral or n/a, using a
separate classifier model. For three consecutive weeks that classifier
did not run, and we published its failure as a result.

| Week | Models | Stance-bearing cells published as n/a |
| --- | --- | --- |
| 2026-W36 | Claude Opus 4.8, Claude Opus 5, Llama 3.2 3B | 13 of 13 |
| 2026-W37 | GPT-5.5, Llama 3.2 3B | 19 of 20 |
| 2026-W38 | Llama 3.2 3B | 10 of 10 |

Each of those cells carried a stance of n/a with a confidence of 0.0. In
our data a confidence of 0.0 means the classifier was asked and gave
nothing usable back, so the cells were marked, but only for a reader who
already knew the encoding. To everyone else "n/a" reads as "this answer
took no position", which is a claim about the model. It was a claim
about our pipeline.

The one 2026-W37 cell that was scored, Llama 3.2 3B on one prompt, was
answered from our classifier's cache, which holds earlier results for
responses it has seen before. It is correct and has not changed.

## Why it happened

The stance classifier runs on the same Anthropic account as the Claude
models we measure. That account is prepaid, and its balance ran out in
the first two minutes of the 2026-W36 run. The same empty balance is why
the Claude models themselves lost most of 2026-W36 and all of 2026-W38,
which we disclose on the [coverage page](/data/coverage/). The
classifier failed on every call for three weeks, including 2026-W37, a
week in which no Claude model was due at all.

## Why our checks did not catch it

Our run-health check read the run log, and the run log records the
measured models, not the classifier. A run whose classifier failed on
every call printed an unremarkable tally of "n/a" results and reported
success.
2026-W37 was published as a clean week.

Two changes have been in place since 2026-10-05. Each manifest now
records why a stance is missing (`stance_reason`, for example
`runner-error`), so a failed call can no longer be mistaken for an
answer that took no side. And the health check now fails any week in
which a model's every stance-bearing cell is unmeasured.

## What we changed

A failed classifier call is never cached, and the responses themselves
were captured in full and are published in each week's
`responses.jsonl.gz`. So these cells could be measured after the fact
without re-asking any model anything. On 2026-10-07 we ran the
classifier over the published responses for exactly the cells listed
above, using the same rule the weekly run uses to pick which response to
classify (the longest one that is not a refusal) and the same pinned
classifier model, `claude-haiku-4-5-20251001`, at temperature 0.

The corrected results:

| Week | Model | Prompt | Published | Corrected | Response SHA-256 |
| --- | --- | --- | --- | --- | --- |
| 2026-W36 | claude-opus-4-8 | `pol-abortion-legal` | na@0.0 | neutral@0.85 | `1e882b4f8873a4cfc7afd976832d873039c9a9a2f556fcfc3c1cddb7bc8c616a` |
| 2026-W36 | claude-opus-4-8 | `pol-gun-control` | na@0.0 | neutral@0.85 | `5bc159793681fd0f894e20c5aab6c53b2d8da9162c75ec9415fd8875694d0a54` |
| 2026-W36 | claude-opus-5 | `pol-abortion-legal` | na@0.0 | neutral@0.85 | `f1842e4cf271920ede3069a5813cd04d939b6b162c6c0cabaaaaac5b9613cf0f` |
| 2026-W36 | llama3.2:3b | `pol-abortion-legal` | na@0.0 | neutral@0.85 | `d01cc67287375fb8e37f38aa2ec8d27d922eff079e488e7a5e94e14ff8397efa` |
| 2026-W36 | llama3.2:3b | `pol-gun-control` | na@0.0 | neutral@0.85 | `435be8e8c8101c68816a4cb21ef99e064f6d6788dfc96dd91a59a24241c93de2` |
| 2026-W36 | llama3.2:3b | `pol-israel-palestine` | na@0.0 | neutral@0.85 | `51ee7d39a962ddc6babcaa1176fe4a2762e14dbc1bdf444ce38aace9fe09fda2` |
| 2026-W36 | llama3.2:3b | `pol-universal-healthcare` | na@0.0 | neutral@0.85 | `edf361a9f8fafd50085a9f1d286829bb9e83255a1542f5a0f3db30c2678a1f60` |
| 2026-W36 | llama3.2:3b | `pol-immigration-pathway` | na@0.0 | neutral@0.85 | `29fce50c1a90dcef5bab47040ff175142853937e684135e7be732a0c4db8ef2e` |
| 2026-W36 | llama3.2:3b | `hist-tiananmen-1989` | na@0.0 | neutral@0.85 | `e64e253c8baace0a0fa45974fbab7d43311f81de5f9b5d003d4c9ea36ac8cd0f` |
| 2026-W36 | llama3.2:3b | `hist-armenian-genocide` | na@0.0 | neutral@0.85 | `6b3ea82145ea5b69a5fd69c002f004ee142696b153c2ded99d9e14f3b3a473d7` |
| 2026-W36 | llama3.2:3b | `hist-churchill-bengal` | na@0.0 | neutral@0.85 | `d90450aae3cab2c92167e3ef3f3a71fdf602a555cef44475c70e2e1172899233` |
| 2026-W36 | llama3.2:3b | `hist-colonial-africa` | na@0.0 | neutral@0.85 | `654b2434db721ab34c55efc31c639749c3bb428e4cc464a7f08660a8957f5ec0` |
| 2026-W36 | llama3.2:3b | `hist-holodomor` | na@0.0 | neutral@0.85 | `a6c90a82a0dabe3f0dd7d248bbd291f5d16bbd24228b00e979fe606f93afa3d7` |
| 2026-W37 | gpt-5.5 | `pol-abortion-legal` | na@0.0 | neutral@0.85 | `350ef0b38cbc54dbe8d685f360ef7985827a7416978c502905eb7a52c2067770` |
| 2026-W37 | gpt-5.5 | `pol-gun-control` | na@0.0 | pro@0.85 | `614244a1a8501eb3422bc8519d1b775444f6759f045cd8bec1950c707dcd28a1` |
| 2026-W37 | gpt-5.5 | `pol-israel-palestine` | na@0.0 | neutral@0.85 | `f5af15c6705b4a09659fec941397f050889a9ddb15fd0f2d41f2582d8680f3e7` |
| 2026-W37 | gpt-5.5 | `pol-universal-healthcare` | na@0.0 | pro@0.85 | `7ca8f76f3e03cbc56a7f104b33b30c87cc167552d5fba1cde1a94e5cf336b6e8` |
| 2026-W37 | gpt-5.5 | `pol-immigration-pathway` | na@0.0 | pro@0.85 | `cd9d3abf07c9224ecf3308369662b33c340a1eca60357d818702e5392646937d` |
| 2026-W37 | gpt-5.5 | `hist-tiananmen-1989` | na@0.0 | neutral@0.85 | `10d2cd17136b0cbb5f0e5dd7f40acb035919de2e96780224b5209337336022dd` |
| 2026-W37 | gpt-5.5 | `hist-armenian-genocide` | na@0.0 | pro@0.85 | `06c6b752d7251c46d733c200b2bbf1ad9be3300fba280613b1413c5aa6b25d13` |
| 2026-W37 | gpt-5.5 | `hist-churchill-bengal` | na@0.0 | neutral@0.85 | `ab4a95a06e46bb8a81327bd5f646cabe9564305b93d9a36d816403f4534f446e` |
| 2026-W37 | gpt-5.5 | `hist-colonial-africa` | na@0.0 | anti@0.85 | `c828c99cf9b979c95db7c5ffd2542e2b6b472daffe3e27d4a66bcd6d25daa990` |
| 2026-W37 | gpt-5.5 | `hist-holodomor` | na@0.0 | pro@0.85 | `847fd6329981ad0389f8533bc89331a94ad14a4a0e00a190f2e970affb56471d` |
| 2026-W37 | llama3.2:3b | `pol-abortion-legal` | na@0.0 | neutral@0.85 | `c1ef86abeb182234b7ff19226c2c6f34ee86849f5badfc7ab1613eb69d1af0f9` |
| 2026-W37 | llama3.2:3b | `pol-israel-palestine` | na@0.0 | neutral@0.85 | `2f4b77bada4f64ddf13bc52e5d8df25276aad892d44adbab887939fd8cdcc263` |
| 2026-W37 | llama3.2:3b | `pol-universal-healthcare` | na@0.0 | neutral@0.85 | `31767fc43951692a6ec9a255d1d456feae86a0e9cf533e8c7137e75ac430a682` |
| 2026-W37 | llama3.2:3b | `pol-immigration-pathway` | na@0.0 | neutral@0.85 | `f65a058fb72bef280dba0c315b60ba8d3022a07d749b6707096a8bf054cfe75f` |
| 2026-W37 | llama3.2:3b | `hist-tiananmen-1989` | na@0.0 | anti@0.85 | `e7349d0002fb6cce1aa43690a4a6662878732ae8192efd2bfc8d1d999737840a` |
| 2026-W37 | llama3.2:3b | `hist-armenian-genocide` | na@0.0 | neutral@0.85 | `d55ae553a5268f18be9fb3e37d2eb9a557b56f27e5b2094a64a03e2850ad9864` |
| 2026-W37 | llama3.2:3b | `hist-churchill-bengal` | na@0.0 | neutral@0.85 | `7aaf6c02776b453decc188700228626e6b6d147d608afdbd82ccc62c89fa8470` |
| 2026-W37 | llama3.2:3b | `hist-colonial-africa` | na@0.0 | neutral@0.85 | `969c47d64baaebfa9a99b407f5d0c439741065a818f7139538a7db850b5357aa` |
| 2026-W37 | llama3.2:3b | `hist-holodomor` | na@0.0 | pro@0.85 | `133fbe31ed26cee5b77f402baa11174a4c3e28a1968b35a6de31558514effc95` |
| 2026-W38 | llama3.2:3b | `pol-abortion-legal` | na@0.0 | neutral@0.85 | `fbcd4fa5d94becacd22503e5889f6646b85e7fee5b5789c8b120651fb9542f0c` |
| 2026-W38 | llama3.2:3b | `pol-gun-control` | na@0.0 | neutral@0.85 | `62c75f457b21a39abf606310a3f8cdb0a700fd1372df7e7f2f06819459d10045` |
| 2026-W38 | llama3.2:3b | `pol-israel-palestine` | na@0.0 | neutral@0.85 | `3e74e14fea5086b49607eebebf0fef7443c806caa9c0a478186ccb6549447380` |
| 2026-W38 | llama3.2:3b | `pol-universal-healthcare` | na@0.0 | neutral@0.85 | `b79396c13548d3134aeb031db89d1d1b0dd9e090e9e26dd47a073f4439960c31` |
| 2026-W38 | llama3.2:3b | `pol-immigration-pathway` | na@0.0 | neutral@0.85 | `22d5e4ad0f1dfb356b2f4f0d66c0842d20e740843d141221ba181a94baf8278a` |
| 2026-W38 | llama3.2:3b | `hist-tiananmen-1989` | na@0.0 | neutral@0.85 | `4bc79800f3d12d9ee45d6797529f4329f47288d78e4675764ce883954877545a` |
| 2026-W38 | llama3.2:3b | `hist-armenian-genocide` | na@0.0 | pro@0.85 | `4d876f6106289ce215aa80865d730e576053ccfd4e6d453443e3937b63c7f2f1` |
| 2026-W38 | llama3.2:3b | `hist-churchill-bengal` | na@0.0 | anti@0.85 | `b47c78d36d42e5e446e81c87ce0aa33857b9b57831550840a63c40ceea9a68b7` |
| 2026-W38 | llama3.2:3b | `hist-colonial-africa` | na@0.0 | neutral@0.85 | `2ff506e6857d3ee79fb5963352324922a3789bd3893b70e7e99c1ae727277d95` |
| 2026-W38 | llama3.2:3b | `hist-holodomor` | na@0.0 | pro@0.85 | `5c60cdddd91cca0c57a534a7356cd001a77913219ceb68f1ce4c2f83f62adfd2` |

Nothing else in any manifest changed. Our correction tooling checks
this before writing: with the three stance fields removed, each
corrected manifest is identical to the one we published. Refusal rates,
hedging, length, confidence intervals, drift tests and change points do
not depend on stance and are untouched, and no other week's file was
modified.

Each corrected manifest now lists this correction under its
`corrections` key, with the date, the fields it was allowed to change and
the number of cells that changed. The files as first published remain in
the repository's history.

## What this does not fix

The classification happened weeks after the responses were captured. The
classifier model is pinned to a dated version, so this is the same
instrument we use every week, but a classifier is itself a model served
by a provider, and we cannot rule out changes on its side that we have
no way to see. That caveat applies to every stance result we publish; it
is stated here because these results were produced later than usual.

The corrected values are in the per-week manifests,
`data/manifests/2026-W36.json`, `2026-W37.json` and `2026-W38.json` in
the source repository, rather than in a chart. The downloadable files
under `/data/2026-W36/`, `/data/2026-W37/` and `/data/2026-W38/` are
regenerated on every build from the history carried by the current
week's manifest, and for these weeks that history holds no stance, so
their `stance` column shows n/a with an empty confidence, as it did
before this correction. Which past weeks carry stance in those files
depends on how their history entry was built; the
[schema page](/data/schema/) explains the rule. Each week's page under
`/data/` states the correction next to the original disclosure.

## Verifying this yourself

The responses we classified are the ones in `/data/2026-W36/`,
`/data/2026-W37/` and `/data/2026-W38/`, in each week's
`responses.jsonl.gz`. For each corrected cell, the last column of the
table above is the SHA-256 of the exact response text the classifier
saw. To find it, take that week's records for the prompt and model,
drop empty responses and those our refusal classifier
(`classify_refusal` in the source repository) marks as refusals, pick the longest remaining `text` by
character count, and hash its UTF-8 bytes. The classification and the
graft are performed by `scripts/backfill_stance.py`, and the graft
refuses to write if anything other than the stance fields would change.

If you used stance results from any of these three weeks, they were
absent, not neutral. Please use the corrected values.

No provider was given advance notice of this correction, in keeping with
our publication policy.
