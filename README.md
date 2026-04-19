# Meridian — Drift Audit

Longitudinal measurement of political, epistemic, and behavioral drift in
deployed commercial large language models. A fixed corpus is queried weekly
against every major provider; responses are stored append-only with full
version metadata; a public dashboard publishes statistically rigorous drift
reports with receipts.

See `CLAUDE.md` for the mission statement and design goals.

## Repository layout

```
meridian/
├── drift_audit/              # the pipeline (Python)
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
git clone https://github.com/drift-audit/meridian
cd meridian
uv sync
```

## Quickstart (no API keys needed)

Ollama is enabled by default in `drift_audit/config.yaml` because it costs
nothing and gives you a local baseline / control group.

```bash
# Install Ollama from https://ollama.com and pull a small model:
ollama pull llama3.2:3b

# Run the pipeline for this week, then rebuild the site from the result:
uv run python -m drift_audit.pipeline.cli run --yes
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
    --manifest site/fixtures/manifest-2026-W16.json --out site/dist
```

## Running against real providers

Add API keys to the environment; the CLI will pick them up automatically.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
```

Then enable the runners in `drift_audit/config.yaml` (set `enabled: true`)
and run:

```bash
# See the estimated cost first.
uv run python -m drift_audit.pipeline.cli estimate

# Run for real (add --yes to skip confirmation):
uv run python -m drift_audit.pipeline.cli run --yes
```

The orchestrator is idempotent: re-running the same week is a no-op. Use
`--force` to sample additional responses on top of the existing file.

## Tests

```bash
uv run pytest                # all tests (pipeline + site + e2e)
uv run pytest drift_audit/   # pipeline only
```

The e2e test (`drift_audit/tests/test_e2e_pipeline_to_site.py`) exercises
the whole stack with a fake runner: no network, no API keys required.

## Contributing

See [`CLAUDE.md`](CLAUDE.md) for the mission, design principles, and hard
rules (no provider funding, public funding sources, append-only raw data,
versioned corpus). See [`drift_audit/runners/README.md`](drift_audit/runners/README.md)
for how to add a new provider. Corpus additions go through GitHub Issues
with the `Prompt proposal` template; see the site's `/contribute/` page.

## License

- Code: MIT (`LICENSE`).
- Corpus, data, and reports: CC BY-SA 4.0 (`LICENSE-DATA`).
