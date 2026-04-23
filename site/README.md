# site/ — Meridian public website source

Static site generator for the Meridian dashboard, reports, and bulk data publication. Built with Python + Jinja2, deployed to GitHub Pages via GitHub Actions.

## Layout

```
site/
├── src/
│   ├── templates/      # Jinja2 templates (base.html + per-page)
│   ├── static/
│   │   ├── css/        # Baseline stylesheet + print + dark mode
│   │   ├── js/         # Progressive-enhancement layer (optional)
│   │   ├── fonts/      # Self-hosted (Inter, IBM Plex Mono)
│   │   └── images/
│   └── build.py        # Renderer — reads manifest, writes dist/
├── schemas/            # Pipeline -> site contract (versioned)
├── fixtures/           # Synthetic manifests for local dev without the pipeline
└── dist/               # Build output (gitignored; deployed to gh-pages)
```

## Build

```
python -m site.src.build --manifest fixtures/manifest-2026-W16.json --out dist/
```

See `.devloop/plan.md` for the full build plan and `.devloop/spikes/meridian-website.md` for the architecture decision record.
