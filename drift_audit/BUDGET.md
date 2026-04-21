# Budget and roster tiers

This document captures the cost / coverage tradeoffs for the Drift Audit
pipeline and the tier ladder we can climb as funding grows. The active
roster and schedule are in `drift_audit/config.yaml`; this file is the
context that explains why that config looks the way it does.

All numbers from `drift_audit.sampling.pricing.estimate_cost` at the
default sampling plan (`n_default_temp=20`, `n_zero_temp=5`,
`avg_input_chars=80`, `avg_output_tokens=500`). Provider pricing as of
2026-04. Actual usage is typically within ±30% of estimate.

---

## Current configuration (Level 0 — alternation)

- Corpus: 30 prompts public
- Ollama `llama3.2:3b` — every week (free, local baseline)
- Claude Opus 4.7 — **even ISO weeks only**
- GPT-5 Preview — **odd ISO weeks only**

**Weekly cost: $0 (Ollama) + $28.35 (Opus) OR $11.40 (Preview), alternating.**
**Monthly average: ~$86 ($1,035/yr.)**

This gives us frontier-model coverage on both OpenAI and Anthropic
without paying for both every week. Ollama produces a continuous
baseline every week so the silent-update detector has a stable
reference. Alternation halves the time-resolution on Opus and Preview
individually — acceptable when drift on the frontier models is a
months-scale story, not a weeks-scale one.

---

## Scaling reference — full roster every week (all 6 paid models)

| Prompts | Per week | **Per month** | Per year |
|---:|---:|---:|---:|
| 30 | $50.95 | **$221** | $2,649 |
| 50 | $84.91 | **$368** | $4,415 |
| 75 | $127.37 | **$552** | $6,623 |
| 100 | $169.82 | **$736** | $8,831 |
| 150 | $254.73 | **$1,104** | $13,246 |
| 200 | $339.64 | **$1,472** | $17,661 |
| 300 | $509.47 | **$2,208** | $26,492 |

Scaling is linear in prompts × samples × tokens × price. Opus + GPT-5
Preview consume ~80% of the bill at every scale.

---

## Combined-lever options at 100 prompts (frontier-model corpus target)

| Config | Per month |
|---|---:|
| N=20 weekly, full roster | $736 |
| N=15 weekly, full roster | $552 |
| N=20 weekly, Opus alternated biweekly | $566 |
| **N=15 weekly, Opus alternated biweekly, held-out monthly** | **~$380** |
| N=20 weekly, cheap-tier only (Haiku + 4.1-mini + Ollama) | ~$25 |

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
- 30 prompts, Ollama every week, Opus + Preview alternating.
- **~$86/mo / $1,035/yr.**
- Hits: both OpenAI and Anthropic frontier, Ollama baseline, at a volunteer-sustainable cost.
- Misses: no mid-tier (Sonnet, Haiku, GPT-4o, 4.1-mini), no Gemini, biweekly granularity on frontier.

### Level 1 — Add the cheap tier every week
- 30 prompts, + Haiku and GPT-4.1-mini every week.
- Additional cost: ~$7.50/mo. Total **~$93/mo.**
- Value: two more per-provider data points, better silent-update coverage, faster accumulation of hand-labelable refusals.
- Suggested trigger: any time; this is basically free.

### Level 2 — Unalternate frontier, add Gemini
- 30 prompts, Opus + Preview every week, + Gemini 2.5 Pro weekly.
- **~$250/mo** (estimated; Gemini pricing TBD in the pricing table).
- Value: weekly frontier granularity, all three major providers, suitable for regular public reporting.
- Suggested trigger: confirmed funding of ~$300/mo or first-run results warrant weekly frontier cadence.

### Level 3 — Expand corpus
- 50–75 prompts on Level 2 roster.
- ~$368–$552/mo.
- Value: credible axis coverage (~10 per axis), approaches CLAUDE.md v0.2 target.
- Suggested trigger: methodology credibility becomes a gating factor for press/research engagement.

### Level 4 — v1.0 target
- 150 prompts on full roster.
- **~$1,100/mo** ($13,250/yr).
- Value: the CLAUDE.md-spec corpus. Journalism-quality evidence base.
- Suggested trigger: grant-funded, or institutional patronage landed.

### Level 5 — CLAUDE.md full spec
- 200–300 prompts on full roster.
- **~$1,500–$2,200/mo.**
- Suggested trigger: sustained institutional funding.

---

---

## What each tier-jump actually requires

These are the sponsor-unlock triggers. None of them are aspirational —
each level is already implemented in code or requires only minor config
changes. The gate is funding.

- **Level 1 (+$7/mo over Level 0)**: enable Haiku + GPT-4.1-mini every
  week. Config change only; no new code.
- **Level 2 (+$165/mo over Level 0)**: unalternate Opus + Preview and
  add Gemini 2.5 Pro. Requires a Gemini runner (not yet implemented)
  and funding for weekly frontier sampling.
- **Level 3 (+$470/mo over Level 0)**: expand corpus from 30 to
  75 prompts. Requires ~45 new curated prompts through the GitHub Issue
  template process (Phase 3.1) plus sustained funding.
- **Level 4 (+$1,000/mo over Level 0)**: full v1.0 corpus (150 prompts)
  plus durable storage (S3 uploader + IPFS pinning, Phase 4). Makes the
  &ldquo;retention forever&rdquo; guarantee real.
- **Level 5 (+$2,100/mo over Level 0)**: CLAUDE.md spec corpus
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
