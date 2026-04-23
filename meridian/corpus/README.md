# meridian/corpus — operator notes

Everything in this directory defines what Meridian is *measuring*.

## Files

| File | Purpose | In git? |
|------|---------|---------|
| `prompts.yaml` | Public corpus. Canonical source. | Yes |
| `corpus.py` | Loader + Pydantic model. | Yes |
| `held_out.example.yaml` | Template for the held-out set. | Yes |
| `held_out.yaml` / `held_out.local.yaml` | Real held-out prompts. Maintainer-only. | **No** (gitignored) |
| `CHANGELOG.md` | Auto-generated prompt-change log. | Yes |
| `README.md` | This file. | Yes |

## Adding a prompt

1. Open a GitHub Issue using the **Prompt proposal** template. Include
   axis, rationale, prior art, and expected stance direction.
2. Maintainers review. Prompts that target a specific provider or that
   seek operationally harmful outputs are rejected (see `/contribute/`).
3. Accepted proposals land in `prompts.yaml` via a PR that bumps
   `corpus_version` to the date of merge (e.g. `2026.06.03-v0.3`).
4. Run `uv run python scripts/corpus_changelog.py > corpus/CHANGELOG.md`
   as part of the merging PR so the changelog stays current.

## Editing an existing prompt

**Do not edit a prompt's text in place.** The `id` + `text_hash` pair
is the invariant longitudinal studies depend on. Instead:

1. Give the replacement a new `id` (e.g. `pol-abortion-legal-v2`).
2. Leave the old entry in `prompts.yaml` for at least one full year so
   ongoing time-series comparisons remain valid.
3. After the transition period, mark the old prompt retired by moving
   it to a `retired_prompts:` section (add to `corpus.py` schema) or
   removing it in the annual rotation PR.

## Held-out protocol

The held-out set has measurement value ONLY because it is not public.
Never commit `held_out.yaml` or `held_out.local.yaml`.

- Target size: ~30% of total corpus (so roughly 45 prompts when the
  public corpus reaches 150).
- Cover every axis the public corpus covers.
- Phrase held-out prompts topically similarly but textually distinctly
  from their public counterparts.
- Keep an offline backup of the held-out set (date-stamped) so the same
  set runs across a full year of weekly snapshots.

## Annual rotation

Once a year:

1. Promote ~10% of the held-out set to public (it has served its
   purpose; publishing it now enables public drift analysis of those
   prompts without compromising the rest of the held-out set).
2. Author replacement held-out prompts to keep the ratio ~30%.
3. Version-bump `prompts.yaml` (e.g. `2027.04.01-v1.0`).
4. Record the rotation in a dated GitHub Issue.

Rotation is a maintainer action. Contributors do not propose rotations
via the prompt-proposal workflow.

## Regenerating the changelog

```bash
uv run python scripts/corpus_changelog.py > meridian/corpus/CHANGELOG.md
```

Review the diff, commit the updated `CHANGELOG.md` alongside whatever
prompt change triggered the regeneration.
