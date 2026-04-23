# Statistical methodology — drift detection

Phase-1 analysis modules each expose a well-defined statistic on a single
(prompt × model × week) bucket: refusal rate, hedge density, length
distribution, embedding centroid, stance. This note specifies how those
per-bucket statistics are combined into **drift decisions** — the claim
that a (prompt × model × metric) changed between weeks — and how the
false-discovery rate of those decisions is controlled.

## The hypothesis family

A weekly drift report issues one decision per (prompt × model × metric)
pair for which a prior week of samples exists. The null hypothesis is
that the metric's underlying distribution is unchanged between the prior
week and this week. Rejection means the report claims drift on that
pair.

A full-scale weekly report covers on the order of 300 prompts × 6 models
× 3 tested metrics ≈ 5,400 simultaneous tests. At an uncorrected α=0.05
the expected number of false positives under complete null would be ~270
per week. That is uninterpretable for a reader. We therefore apply
Benjamini–Hochberg (`meridian.analysis.multiple_testing.bh_correct`)
at FDR=0.05 across the within-week family. BH controls the expected
share of false rejections among claimed drift findings; it is the right
target when the project wants to surface many true drifts and can
tolerate a small fraction of false ones.

The family is within-week. We do not pool tests across weeks because
change-point detection (see `change_point.py`) is a separate,
complementary analysis that operates on a time series rather than a
pairwise test. BH and change-point detection answer different questions
and are displayed separately on the site.

## Per-metric tests

Each test takes this week's samples and the prior week's samples for the
same (prompt × model) and returns one p-value.

- **Refusal rate.** Per-sample Bernoulli (each sample is or is not a
  refusal, per `refusal.classify_refusal`). Test: two-proportion z-test
  when both weeks have n ≥ 30; bootstrap tail probability of the
  difference in means otherwise. The bootstrap variant resamples with
  replacement from the pooled samples under the null and returns
  `2 · min(P(Δ̂_null ≥ Δ̂_obs), P(Δ̂_null ≤ Δ̂_obs))`.
- **Hedge density.** Per-sample score computed by calling
  `hedge.hedge_density(sample.text)` on each sample — which produces
  hedging markers per 100 tokens for that sample. Test: bootstrap
  two-sample difference in means, as above.
- **Length.** Per-sample word count (whitespace tokens). Test: bootstrap
  two-sample difference in medians, as above.

Bootstrap rounds: 1,000 (same as
`meridian.analysis.confidence.bootstrap_ci`). Deterministic when a
seed is passed through from `build_manifest`.

## No-prior-week handling

When a (prompt × model) pair has no prior week in storage, the pair is
**excluded from the BH family** — not p-value = 1.0. Including untested
pairs at p=1 would inflate the denominator `n` in BH's `k/n · α`
threshold and weaken real signal. The metric record in that case simply
omits `p_value`, `adjusted_p_value`, and `significant_after_bh`.

## Output on the metric record

Each `MetricRecord` carries one p-value per tested metric, the
BH-adjusted p-value, and the rejection decision:

```json
{
  "prompt_id": "...", "model_id": "...",
  "refusal_rate": 0.12, "refusal_ci": { "lower": ..., "upper": ... },
  "refusal_p_value": 0.003, "refusal_adjusted_p_value": 0.021,
  "refusal_significant_after_bh": true,

  "hedge_density": 1.4,
  "hedge_p_value": 0.34, "hedge_adjusted_p_value": 0.62,
  "hedge_significant_after_bh": false,

  "length_p_value": 0.07, "length_adjusted_p_value": 0.22,
  "length_significant_after_bh": false,

  // ... existing fields (stance, embedding_centroid_shift, flagged_for_review, etc.)
}
```

The site consumes `*_significant_after_bh` to decide visual emphasis
(badge, color); researchers reading the CSV/parquet export get all three
columns and can re-run BH themselves to verify.

## Why not family-wise (Bonferroni / Holm)

Family-wise error control treats any single false positive as
catastrophic. For a public monitoring project whose job is to surface
many simultaneous drifts, FWER is too conservative — a single true
refusal-rate shift on a controversial prompt would fail to reach
significance after dividing α by 5,000. BH trades a bounded share of
false positives for dramatically higher statistical power on the true
positives, which matches this project's goals.

## What this does not cover

- **Stance drift.** Stance is a categorical output with a confidence
  score rather than a two-sample test. Rendered on the dashboard as a
  qualitative change indicator (stance this week vs. prior week); not
  part of the BH family.
- **Embedding centroid shift.** Continuous distance measure; shown on
  the dashboard but not reduced to a null-hypothesis test. Change-point
  detection on the centroid-shift time series is the complementary
  analysis.
- **Silent-update warnings** from `silent_update.py`. Those flag
  anomalies on neutral-control axes (the model should not be changing
  there) and are not drift findings; they are published as a separate
  `silent_update_warnings` block on the manifest.
