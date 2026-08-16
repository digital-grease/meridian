# Budget and roster tiers

This document captures the cost / coverage tradeoffs for the Meridian
pipeline and the tier ladder we can climb as funding grows. The active
roster and schedule are in `meridian/config.yaml`; this file is the
context that explains why that config looks the way it does.

All numbers from `meridian.sampling.pricing.estimate_cost` at the
default sampling plan (`n_default_temp=20`, `n_zero_temp=5`,
`avg_input_chars=80`) and at the completion caps in
`meridian/config.yaml` (shared 1024, gpt-5.5 pinned to 8192). Provider
pricing as of 2026-06.

Two things changed in 2026-08 and every figure below is on the new
basis. There is no longer a flat `avg_output_tokens=500`: expected
billed output is derived from each runner's completion cap, because
reasoning tokens bill against that cap and gpt-5.5's was raised from
1024 to 8192 to fix the truncated-completion bug. And the estimator now
reads the temperature plan, so it stops pricing the zero-temp batch for
models that reject `temperature=0` and never receive it (both paid
models in the current roster). Read
`meridian/sampling/pricing.py`'s module docstring for the model and its
calibration receipts before quoting any number here.

The estimate is deliberately conservative and runs above observed
actuals, because it gates `run --max-cost` and erring low is the failure
that costs money. It is not a spend forecast: the Opus week below
estimates $8.35 against the $7.08 that 2026-W28 actually billed, and the
gap widens with the cap. Set a ceiling against the estimate, not against
what you expect the invoice to say.

---

## Current configuration (Level 0 — alternation)

- Corpus: 30 prompts public
- Ollama `llama3.2:3b` — every week (free, local baseline)
- Claude Opus 4.8 — **even ISO weeks only**
- Claude Opus 5 — **even ISO weeks only**, alongside 4.8 rather than
  replacing it, so the 4-8 series continues and the two versions are
  compared within a week rather than across one
- GPT-5.5 — **odd ISO weeks only**

**Weekly cost: $0 (Ollama) + $27.45 (even: $8.35 Opus 4.8 + $19.10 Opus 5)
OR $22.91 (odd: GPT-5.5).**
**Monthly average: ~$109 ($1,309/yr.)**

Both alternating weeks now sit under the $40 `--max-cost` ceiling in
`scripts/run-weekly.sh`, the even week at 69% of it. That margin is
thinner than it looks: raising Opus 5's completion cap, or adding a
fourth paid runner, would put an even week through the ceiling and stop
the run rather than overspend. Raise the ceiling deliberately if either
happens; do not raise it in response to an abort without checking which.

The GPT-5.5 week is now the expensive one, which reverses the old
ordering. That is the 8192 completion cap: gpt-5.5 bills reasoning
tokens against it, so the same 600 calls carry roughly 1,270 expected
billed output tokens each against Opus's 552 at the shared 1024. The old
figures here ($9.45 / $11.32, ~$45/mo) predate both the cap change and
the cap-aware estimator and should not be quoted.

This gives us frontier-model coverage on both OpenAI and Anthropic
without paying for both every week. Ollama produces a continuous
baseline every week so the silent-update detector has a stable
reference. Alternation halves the time-resolution on Opus and GPT-5.5
individually — acceptable when drift on the frontier models is a
months-scale story, not a weeks-scale one.

---

## Scaling reference — full roster every week (all 6 paid models)

| Prompts | Per week | **Per month** | Per year |
|---:|---:|---:|---:|
| 30 | $42.69 | **$185** | $2,220 |
| 50 | $71.14 | **$308** | $3,699 |
| 75 | $106.70 | **$462** | $5,548 |
| 100 | $142.26 | **$616** | $7,398 |
| 150 | $213.41 | **$925** | $11,097 |
| 200 | $284.54 | **$1,233** | $14,796 |
| 300 | $426.81 | **$1,850** | $22,194 |

Roster priced here: Opus 4.8, Sonnet 4.6, Haiku 4.5, GPT-5.5, GPT-4o,
GPT-4.1-mini, at the config's caps (gpt-5.5 at 8192, the rest at 1024).
Scaling is linear in prompts × samples × tokens × price. Opus + GPT-5.5
consume ~73% of the bill at every scale, up from ~65% before the gpt-5.5
cap change; GPT-5.5 alone is 54%.

---

## Combined-lever options at 100 prompts (frontier-model corpus target)

> Not yet re-derived on the 2026-08 basis. Every figure in this section
> and in the tier ladder below still carries the pre-2026-08 flat-500
> arithmetic at the shared 1024 cap, so it reads low by roughly a third
> wherever GPT-5.5 appears. The relative ordering of the options is
> unaffected, which is what the section is for. Re-derive before quoting
> an absolute number anywhere public:
> `uv run python -m meridian.pipeline.cli estimate`.

| Config | Per month |
|---|---:|
| N=20 weekly, full roster | $369 |
| N=15 weekly, full roster | $277 |
| N=20 weekly, Opus alternated biweekly | $315 |
| **N=15 weekly, Opus alternated biweekly, held-out monthly** | **~$180** |
| N=20 weekly, cheap-tier only (Haiku + 4.1-mini + Ollama) | ~$20 |

The penultimate option is the recommended target when the corpus grows.

---

## Levers that reduce cost without dropping models

| Lever | Effect |
|---|---|
| Cut `n_default_temp` from 20 → 10 | ~40% off. N=10 still gives usable CIs on binary events (refusal); loses statistical power on continuous metrics (hedge density, length). |
| Drop the `n_zero_temp=5` deterministic baseline | ~20% off. Loses one of the sanity checks against temperature noise. |
| Alternate a specific model every other week (the current Level 0 approach) | ~22% off for the alternated model. Gaps halve the time-resolution on that model. |
| Run held-out set monthly instead of weekly | ~25% off. Held-out still fulfills benchmark-targeting detection at lower cadence. |

---

## Tier ladder for future upgrades

### Level 0 — Current: alternation
- 30 prompts, Ollama every week, Opus + GPT-5.5 alternating.
- **~$68/mo / $813/yr.** (On the 2026-08 basis. The levels below are
  still on the old one, see the note above; their *deltas* from Level 0
  remain roughly right, their totals do not.)
- Hits: both OpenAI and Anthropic frontier, Ollama baseline, at a volunteer-sustainable cost.
- Misses: no mid-tier (Sonnet, Haiku, GPT-4o, 4.1-mini), no Gemini, biweekly granularity on frontier.

### Level 1 — Add the cheap tier every week
- 30 prompts, + Haiku and GPT-4.1-mini every week.
- Additional cost: ~$7.50/mo. Total **~$53/mo.**
- Value: two more per-provider data points, better silent-update coverage, faster accumulation of hand-labelable refusals.
- Suggested trigger: any time; this is basically free.

### Level 2 — Unalternate frontier, add Gemini
- 30 prompts, Opus + GPT-5.5 every week, + Gemini 2.5 Pro weekly.
- **~$90/mo before Gemini** (Gemini pricing TBD in the pricing table).
- Value: weekly frontier granularity, all three major providers, suitable for regular public reporting.
- Suggested trigger: confirmed funding of ~$300/mo or first-run results warrant weekly frontier cadence.

### Level 3 — Expand corpus
- 50–75 prompts on Level 2 roster.
- ~$231–$346/mo before Gemini.
- Value: credible axis coverage (~10 per axis), approaches CLAUDE.md v0.2 target.
- Suggested trigger: methodology credibility becomes a gating factor for press/research engagement.

### Level 4 — v1.0 target
- 150 prompts on full roster.
- **~$700/mo** ($8,300/yr) before Gemini.
- Value: the CLAUDE.md-spec corpus. Journalism-quality evidence base.
- Suggested trigger: grant-funded, or institutional patronage landed.

### Level 5 — CLAUDE.md full spec
- 200–300 prompts on full roster.
- **~$900–$1,400/mo** before Gemini.
- Suggested trigger: sustained institutional funding.

---

---

## What each tier-jump actually requires

These are the sponsor-unlock triggers. None of them are aspirational —
each level is already implemented in code or requires only minor config
changes. The gate is funding.

- **Level 1 (+$8/mo over Level 0)**: enable Haiku + GPT-4.1-mini every
  week. Config change only; no new code.
- **Level 2 (+$45/mo over Level 0, before Gemini)**: unalternate Opus + GPT-5.5 and
  add Gemini 2.5 Pro. Requires a Gemini runner (not yet implemented)
  and funding for weekly frontier sampling.
- **Level 3 (+$300/mo over Level 0, before Gemini)**: expand corpus from 30 to
  75 prompts. Requires ~45 new curated prompts through the GitHub Issue
  template process (Phase 3.1) plus sustained funding.
- **Level 4 (+$650/mo over Level 0, before Gemini)**: full v1.0 corpus (150 prompts)
  plus durable storage (S3 uploader + IPFS pinning, Phase 4). Makes the
  &ldquo;retention forever&rdquo; guarantee real.
- **Level 5 (+$1,300/mo over Level 0, before Gemini)**: CLAUDE.md spec corpus
  (200&ndash;300 prompts) plus trained refusal classifier (Phase 5.4)
  plus Postgres index for researchers (Phase 4.6).

Each level is cumulative; Level 4 implies Levels 0&ndash;3.

See `/funding/` on the deployed site for the public-facing version of
this ladder.

---

## Reminder: hard rules the budget cannot bend

- No funding from LLM providers, ever.
- Funding sources public and prominent (see `/funding/`).
- No preferential treatment to funders in analysis.
- API is the measurement surface. Browser-surface scraping is not a
  cost-reduction avenue — see `.devloop/spikes/browser-surface.md`.

---

## Storage-size ceiling on in-repo weekly snapshots

The pipeline emits `data/snapshots/{week}/responses.jsonl.gz` each run
and the weekly-pipeline workflow commits it back to `main`. For the
current Level-0 corpus this is ~1&ndash;3&nbsp;MB per week, trivially
manageable in git.

At larger corpus sizes the in-repo approach stops scaling:

| Level | Per-week gzip | Per-year accumulation |
|---|---:|---:|
| 0 &mdash; 30 prompts, 3 models | ~1&ndash;3&nbsp;MB | ~50&ndash;150&nbsp;MB |
| 3 &mdash; 75 prompts, 6 models | ~5&ndash;15&nbsp;MB | ~250&ndash;780&nbsp;MB |
| 4 &mdash; 150 prompts, 6 models | ~10&ndash;30&nbsp;MB | ~500&nbsp;MB&ndash;1.5&nbsp;GB |
| 5 &mdash; 300 prompts, 6 models | ~20&ndash;60&nbsp;MB | ~1&ndash;3&nbsp;GB |

GitHub&rsquo;s soft repo-size limit is 1&nbsp;GB; the hard limit is
about 5&nbsp;GB. We need to migrate snapshots out of git before Level 4
lands. The two ready-made options:

1. GitHub Actions artifacts passed between `weekly-pipeline` and
   `weekly-build` (native, no new infra).
2. S3 archival bucket (already provisioned via `infra/terraform/s3`).
   The bucket is currently private; serving publicly either means
   opening a prefix or having `weekly-build` pull via OIDC and re-host
   on GitHub Pages.

Either path is a ~1-day change when the scale trigger hits. Track the
repo&rsquo;s `.git` size at each level-jump; migrate preemptively when
the 12-month projection crosses 700&nbsp;MB.

---

## Site weight (built `dist/`)

`.github/workflows/weekly-build.yml` fails the build when `site/dist`
exceeds 800&nbsp;MB. Current contribution breakdown:

| Asset | Current | At Level 4 (150 prompts) | Notes |
|---|---:|---:|---|
| OG PNGs (1200&times;630) | ~1.5&nbsp;MB (44 images) | ~5&nbsp;MB (~170 images) | One per prompt/model/axis/report plus index &amp; methodology. See `site/src/og.py`; emission is best-effort and skips when matplotlib is missing. |
| Self-hosted fonts (Inter&nbsp;+&nbsp;Plex) | ~0.7&nbsp;MB | ~0.7&nbsp;MB | Fixed; all weights in one variable WOFF2 each. |
| Per-week `/data/{week}/*` | ~50&nbsp;KB&ndash;3&nbsp;MB | ~5&ndash;30&nbsp;MB | Grows linearly in corpus size &times; week count. |
| HTML (all pages) | ~2&nbsp;MB | ~8&nbsp;MB | Primarily prompt and model pages. |

The per-week artifact is the only linearly-growing axis; everything
else is proportional to corpus breadth, not run count. The OG-image
contribution stays well under the cap even at Level 5 (~300 prompts,
~5&ndash;10&nbsp;MB of PNG). Revisit when the 12-month `dist/`
projection crosses 500&nbsp;MB.
