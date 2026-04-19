# Launch readiness checklist

Hard gates that must all pass before the site goes public. Re-run on every
release candidate. The weekly-build workflow automates most of these; the
checklist records manual sign-off.

## Automated gates (CI)

- [ ] `uv run python site/src/build.py` succeeds with the latest manifest
- [ ] Link-rot guard passes: no previously-published URL is absent from the new build
- [ ] Site size is under 800 MB (the workflow fails above this soft cap)
- [ ] Sitemap is populated (`dist/sitemap.xml` exists and contains canonical URLs)
- [ ] `/robots.txt` is permissive and points at the sitemap
- [ ] Atom feed validates (`feed.xml` parses with a strict parser)
- [ ] `axe-core` reports zero errors on: `/`, `/about/`, `/methodology/`, a representative report, a representative axis page, a representative model page

## Manual gates (pre-launch)

- [ ] Funding disclosure page reflects the *actual* funding situation, not a placeholder
- [ ] `/methodology/reproduce/` walkthrough has been verified end-to-end against a published snapshot
- [ ] Keyboard-only navigation works for every interactive element (tab order, focus visibility, skip link)
- [ ] Screen-reader pass: NVDA (Windows) or VoiceOver (macOS) announces headings, tables, sparklines, and breadcrumbs correctly
- [ ] Color-contrast audit on both light and dark modes (WCAG 2.2 AA)
- [ ] Print stylesheet produces a clean printable report from at least one dashboard page and one report page
- [ ] Print output includes the build-provenance footer on every page
- [ ] Print output includes expanded URLs for links (already in `@media print`)

## Legal and editorial gates

- [ ] Licenses are correct: MIT on `LICENSE` for code, CC-BY-SA 4.0 on `LICENSE-DATA` for data/reports
- [ ] No provider names, quotes, or screenshots are used without disclaimer
- [ ] Editor-in-chief has reviewed the first three published reports for tone and factual accuracy
- [ ] `/about/` accurately lists contributors with disclosures
- [ ] Security and press email addresses on `/about/` resolve to real inboxes

## Things deliberately deferred to v1.1

- **Per-page dynamic OG PNG images.** v1 ships with a static SVG placeholder.
  The dynamic generator requires matplotlib and is tracked separately.
- **`responses.jsonl.gz` and `metrics.parquet` in snapshots.** Requires real pipeline output and pyarrow.
- **IPFS / Arweave pins.** Publish-path integration is pending.
- **Held-out corpus validation runs.** Gated on corpus v1.5.
- **Live query API.** Scoped out of v1; `/data/` bulk snapshots cover documented use cases.
