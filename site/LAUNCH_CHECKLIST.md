# Launch readiness checklist

Hard gates that must all pass before the site goes public. Re-run on every
release candidate. The weekly-build workflow automates most of these; the
checklist records manual sign-off.

**Last walked: 2026-04-22 against commit `ec1c065`-ish (post-rebrand).**

## Automated gates (CI)

- [x] `uv run python site/src/build.py` succeeds with the latest manifest
      *(63 URLs emitted from `site/fixtures/synthetic-fixture.json`).*
- [x] Link-rot guard passes: no previously-published URL is absent from the new build
      *(workflow now fetches `urls.txt` from the live site at
      `meridianaudit.org/urls.txt`; the old gh-pages-branch diff silently
      no-op'd because `deploy-pages@v4` doesn't create that branch).*
- [x] Site size is under 800 MB (the workflow fails above this soft cap)
      *(current build: ~4.2 MB).*
- [x] Sitemap is populated (`dist/sitemap.xml` exists and contains canonical URLs)
      *(59 `<loc>` entries, all pointing at `https://meridianaudit.org/…`).*
- [x] `/robots.txt` is permissive and points at the sitemap
- [x] Atom feed validates (`feed.xml` parses with a strict parser)
      *(3 entries, root is `{http://www.w3.org/2005/Atom}feed`).*
- [x] `axe-core` reports zero errors on: `/`, `/about/`, `/methodology/`, a representative report, a representative axis page, a representative model page
      *(enforced by `meridian/tests/test_accessibility.py` (`@pytest.mark.slow`)
      and `.github/workflows/weekly-build.yml` — both serve `dist/` over HTTP
      rather than `file://`, which was producing ~440 spurious color-contrast
      violations from unresolved root-relative paths).*

## Manual gates (pre-launch)

- [x] Funding disclosure page reflects the *actual* funding situation, not a placeholder
      *(`/funding/` lists current $86/mo Level-0 config, names the hard rules,
      and maps the dollar-tier ladder to concrete corpus/roster upgrades).*
- [x] `/methodology/reproduce/` walkthrough has been verified end-to-end against a published snapshot
      *(exercised on 2026-04-22: `curl
      meridianaudit.org/data/2026-W16/manifest.json` → local build → diff vs
      live site-build output. Only the build timestamp differs, matching the
      documented guarantee. The walkthrough is the `#reproduce` anchor on
      `/methodology/`, not a separate subpage — the checklist wording is
      historical.)*
- [ ] Keyboard-only navigation works for every interactive element (tab order, focus visibility, skip link)
      *(static inspection passes: `.skip-link` focus-visible flips `left:0`;
      nav/footer links are native `<a>`; `.chip` buttons have
      `aria-pressed` + `:focus-visible` outlines; citation `<details>` are
      keyboard-native; the copy button lives as a sibling of `<summary>`
      per the Phase-1 accessibility fix. Still wants a human tab-through
      on the deployed site.)*
- [ ] Screen-reader pass: NVDA (Windows) or VoiceOver (macOS) announces headings, tables, sparklines, and breadcrumbs correctly
- [ ] Color-contrast audit on both light and dark modes (WCAG 2.2 AA)
      *(axe covers whatever mode Chrome starts in — defaults light. Dark
      palette values (`--bg #111214`, `--fg #ededed`, `--fg-muted #a8a8a5`,
      `--link #7ab8ff`, `--link-visited #bfa4ff`) manually checked against
      AA (all > 7:1). CI dark-mode emulation is a nice-to-have; add via
      `@axe-core/puppeteer` + `emulateMediaFeatures` when it becomes
      worth the complexity.)*
- [ ] Print stylesheet produces a clean printable report from at least one dashboard page and one report page
      *(CSS has `@media print` with forced light palette, hidden skip link,
      hidden BMC button / footer-support row, underlined links with
      inline expanded `href`, page-break hints on headings/tables/figures.
      Needs a human smoke-test from a real browser.)*
- [x] Print output includes the build-provenance footer on every page
      *(`.build-prov` is rendered by `base.html` and print CSS styles it
      with a top border; no `display:none` kills it in print.)*
- [x] Print output includes expanded URLs for links (already in `@media print`)
      *(`a[href]::after { content: " (" attr(href) ")"; }`).*

## Legal and editorial gates

- [x] Licenses are correct: MIT on `LICENSE` for code, CC-BY-SA 4.0 on `LICENSE-DATA` for data/reports
      *(both files were missing from the repo root before 2026-04-22; now
      present and referenced by README.)*
- [ ] No provider names, quotes, or screenshots are used without disclaimer
      *(current reports (`site/content/reports/*.md`) are editorial drafts;
      review when reports contain real-pipeline outputs.)*
- [ ] Editor-in-chief has reviewed the first three published reports for tone and factual accuracy
- [ ] `/about/` accurately lists contributors with disclosures
- [x] Security and press email addresses on `/about/` resolve to real inboxes
      *(both consolidated to `dg@digitalgrease.net` 2026-04-25 — wired
      to a maintainer-controlled inbox; revisit when traffic warrants
      separate `press@`/`security@` addresses.)*

## Things deliberately deferred to v1.1

- ~~**Per-page dynamic OG PNG images.** v1 ships with a static SVG placeholder.~~
  **Shipped 2026-04-21.** `site/src/og.py` renders 44 PNGs at build time;
  `base.html` gates on `og_available` and falls back to the SVG when
  matplotlib is missing.
- ~~**`responses.jsonl.gz` and `metrics.parquet` in snapshots.** Requires real pipeline output and pyarrow.~~
  **metrics.parquet shipped 2026-04-19** (best-effort, skips cleanly when
  pyarrow isn't installed). `responses.jsonl.gz` shipped same window.
- **IPFS / Arweave pins.** Publish-path integration is still pending;
  blocked on pinning-service credentials.
- **Held-out corpus validation runs.** Still gated on populating
  `meridian/corpus/held_out.yaml` (deliberately untracked).
- **Live query API.** Scoped out of v1; `/data/` bulk snapshots cover
  documented use cases.
