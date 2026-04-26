# Meridian

**Live at [meridianaudit.org](https://meridianaudit.org).**

<a href="https://www.buymeacoffee.com/digitalgrease" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-red.png" alt="Buy Me A Coffee" style="height: 60px !important;width: 217px !important;" ></a>

Longitudinal measurement of political, epistemic, and behavioral drift in
deployed commercial large language models. A fixed corpus is queried weekly
against every major provider; responses are stored append-only with full
version metadata; a public dashboard publishes statistically rigorous drift
reports with receipts.

The project is volunteer-maintained and API-cost-dominated. Current
runtime is a **$86/mo** Level 0 configuration: 30 prompts, Claude Opus
and GPT-5 Preview alternating biweekly, Ollama as a weekly baseline.
Broadening the corpus, unalternating the frontier models, adding Gemini,
and standing up durable S3 / IPFS / Postgres storage each require
sustained monthly sponsorship. Full tier ladder at
[`meridian/BUDGET.md`](meridian/BUDGET.md). If the record is
useful to you, a coffee via the button above meaningfully extends how
many weeks the project can keep running. We never take funding from an
LLM provider.

## Why this matters

LLMs are increasingly the default knowledge interface. Whatever a model
declines to discuss, reframes, caveats, or presents as settled &mdash; at
scale &mdash; shapes what many people consider thinkable. Training-data
curation is ideological curation; RLHF is normative curation. These
choices are currently invisible to the public and change without notice
or changelog.

A public drift record converts opaque decisions into measurable facts:
useful for journalists covering AI policy, researchers studying
alignment, regulators evaluating concentration risk, and people making
informed choices about which tools to depend on.

## Hard rules

- Raw data is append-only; retention forever.
- Corpus changes are versioned transactions, never in-place edits.
- No provider gets a preview of a report before publication.
- Funding sources are public and prominent.
- The project never takes paid placement, sponsored content, or
  preferential treatment in analysis.

See `/methodology/` on the deployed site for the full design doc.

## Repository layout

```
meridian/
├── meridian/              # the pipeline (Python)
│   ├── corpus/               # versioned prompt corpus (YAML)
│   ├── runners/              # one adapter per provider
│   ├── sampling/             # orchestrator + cost estimator
│   ├── analysis/             # refusal / hedge / length / bootstrap CI
│   ├── storage/              # append-only JSONL local store
│   ├── pipeline/             # CLI + manifest writer
│   └── tests/
├── site/                     # the public website (static, Python + Jinja2)
│   ├── src/                  # build.py, templates/, static/
│   ├── schemas/              # pipeline↔site contract (JSON Schema)
│   ├── fixtures/             # synthetic manifests for bootstrap
│   ├── content/reports/      # authored markdown reports
│   └── LAUNCH_CHECKLIST.md
├── scripts/                  # one-off tooling (fixture gen, schema export, linkrot)
├── .devloop/                 # plan.md, archived plans, spike reports
├── .github/workflows/        # weekly build + deploy
└── pyproject.toml            # single source of truth for deps (uv-managed)
```

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/digital-grease/meridian
cd meridian
uv sync
```

## Quickstart (no API keys needed)

Ollama is enabled by default in `meridian/config.yaml` because it costs
nothing and gives you a local baseline / control group.

```bash
# Install Ollama from https://ollama.com and pull a small model:
ollama pull llama3.2:3b

# Run the pipeline for this week, then rebuild the site from the result:
uv run python -m meridian.pipeline.cli run --yes
uv run python site/src/build.py \
    --manifest site/fixtures/manifest-$(date -u +'%Y-W%V').json \
    --out site/dist

# Preview the site:
python -m http.server 8000 --directory site/dist
```

If you don't want to install Ollama, the site still builds from the synthetic
fixture:

```bash
uv run python scripts/generate_fixture.py
uv run python site/src/build.py \
    --manifest site/fixtures/synthetic-fixture.json --out site/dist
```

## Held-out corpus

The held-out corpus is the project's primary defense against
benchmark-targeting — the risk that a provider specifically optimizes
against prompts it knows are in this corpus. Its measurement value is
that it *never* reaches a provider's training data.

**Committed to this repo:**
- `meridian/corpus/prompts.yaml` — the public corpus.
- `meridian/corpus/held_out.example.yaml` — a template with the
  expected format.

**Never committed:**
- `meridian/corpus/held_out.yaml` (or `.local.yaml`) — your real
  held-out prompts. Both filenames are git-ignored.
- `data/internal/` — internal manifests that contain held-out metrics.
  Also git-ignored.

To populate a held-out set, copy the example to `held_out.yaml`, replace
the prompts, and rerun the pipeline. The CLI will automatically:

1. Sample both public and held-out prompts into storage.
2. Write a public manifest to `site/fixtures/` with held-out **excluded**.
3. Write an internal manifest to `data/internal/` with held-out **included**.

Compare public vs held-out drift:

```bash
uv run python -m meridian.pipeline.cli holdout-report --week 2026-W16
```

If public drift diverges significantly from held-out drift, that gap is
evidence of benchmark-targeting and is itself a publishable finding.

The site build refuses to run (exit non-zero, loud error) if any
held-out prompt ever leaks into the public manifest it reads. It is a
belt-and-suspenders check on top of the manifest writer's own guard.

## Running against real providers

Add API keys to the environment; the CLI will pick them up automatically.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
```

Then enable the runners in `meridian/config.yaml` (set `enabled: true`)
and run:

```bash
# See the estimated cost first.
uv run python -m meridian.pipeline.cli estimate

# Run for real (add --yes to skip confirmation):
uv run python -m meridian.pipeline.cli run --yes
```

The orchestrator is idempotent: re-running the same week is a no-op. Use
`--force` to sample additional responses on top of the existing file.

## Tests

```bash
uv run pytest                # all tests (pipeline + site + e2e)
uv run pytest meridian/   # pipeline only
```

The e2e test (`meridian/tests/test_e2e_pipeline_to_site.py`) exercises
the whole stack with a fake runner: no network, no API keys required.

## Contributing

Hard rules are listed above; the full methodology lives at
`/methodology/` on the deployed site. See
[`meridian/runners/README.md`](meridian/runners/README.md) for how
to add a new provider. Corpus additions go through GitHub Issues with
the `Prompt proposal` template; see the site's `/contribute/` page.

## License

- Code: MIT (`LICENSE`).
- Corpus, data, and reports: CC BY-SA 4.0 (`LICENSE-DATA`).
