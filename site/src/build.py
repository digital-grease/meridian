#!/usr/bin/env python3
"""Meridian static site builder.

Consumes a validated manifest JSON and renders the site to dist/.
Idempotent and deterministic: given the same inputs, produces byte-identical output
(modulo the build timestamp, which is surfaced in build.json for provenance).

Run:
    uv run python site/src/build.py --manifest site/fixtures/synthetic-fixture.json \
        --out site/dist
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_lib
import io
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import markdown
import nh3
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup
from pydantic import ValidationError

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
# Repo root too, so `from meridian...` imports resolve when the
# script is launched as `python site/src/build.py`.
_REPO_ROOT_FOR_IMPORTS = _HERE.parent.parent
if str(_REPO_ROOT_FOR_IMPORTS) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_FOR_IMPORTS))

import excerpts  # noqa: E402
from chart import OKABE_ITO, heatmap_cell_style, sparkline, viridis_color  # noqa: E402
from schema import SCHEMA_VERSION, Manifest, is_measured  # noqa: E402

TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"
REPO_ROOT = _HERE.parent.parent
CONTENT_REPORTS = REPO_ROOT / "site" / "content" / "reports"

SITE_ORIGIN = "https://meridianaudit.org"

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Sanitization allowlist for Markdown-rendered report bodies.
# Report content is authored in-repo and reviewed via PR, but nh3 gives
# defense-in-depth and explicitly documents what markup is permitted.
_ALLOWED_TAGS: set[str] = {
    "a", "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "strong", "em", "code", "pre",
    "blockquote", "time", "hr", "br", "dl", "dt", "dd",
    "figure", "figcaption", "img", "table", "thead", "tbody",
    "tr", "th", "td", "caption", "sup", "sub",
}
_ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title", "id"},
    "time": {"datetime"},
    "img": {"src", "alt", "title", "width", "height"},
    "code": {"class"},
    "pre": {"class"},
    "h1": {"id"}, "h2": {"id"}, "h3": {"id"},
    "h4": {"id"}, "h5": {"id"}, "h6": {"id"},
}
_URL_SCHEMES: set[str] = {"http", "https", "mailto"}

# (template_name, output_path). Directory-indexed paths give clean URLs like /about/.
STATIC_PAGES: list[tuple[str, str]] = [
    ("index.html", "index.html"),
    ("methodology.html", "methodology/index.html"),
    ("corpus.html", "corpus/index.html"),
    ("about.html", "about/index.html"),
    ("funding.html", "funding/index.html"),
    ("contribute.html", "contribute/index.html"),
    # Internal triage surface for silent-update + insufficient-data flags.
    # Unlinked from public nav; template sets <meta name="robots" content="noindex">.
    ("review.html", "review/index.html"),
    # Internal pipeline-health dashboard. Consumes data/run_log.jsonl;
    # template renders empty-state copy when the log is absent.
    ("internal/health.html", "internal/health/index.html"),
    ("404.html", "404.html"),
    # Small JSON manifest for install-to-home-screen on mobile.
    ("site.webmanifest", "site.webmanifest"),
]


def git_sha(repo: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "unknown"


def load_manifest(path: Path) -> Manifest:
    raw = json.loads(path.read_text())
    found = raw.get("schema_version")
    if found != SCHEMA_VERSION:
        raise SystemExit(
            f"schema_version mismatch: manifest reports {found!r}, "
            f"site expects {SCHEMA_VERSION}. Regenerate manifest or update site."
        )
    try:
        manifest = Manifest.model_validate(raw)
    except ValidationError as e:
        raise SystemExit(f"manifest validation failed:\n{e}") from e

    # The public site must never render a held-out prompt. If the pipeline
    # ever handed us one, refuse to build — the alternative is publishing
    # the text of a prompt whose whole value is not being public.
    leaked = [p.prompt_id for p in manifest.prompts if p.held_out]
    if leaked:
        raise SystemExit(
            "refusing to build: held-out prompt(s) found in public manifest: "
            f"{leaked}. Regenerate with include_held_out=False."
        )
    return manifest


def _unmeasured_lookup(model_id: str, manifest, prompt_id: str):
    """Jinja filter: the UnmeasuredCell for this (prompt, model), or None.

    A template cannot tell "this model was not run this week" from "this
    model was run and every response was unusable" by looking at the
    metrics alone, because both produce an absent MetricRecord. Only the
    manifest's ``unmeasured`` list carries the difference.
    """
    return manifest.unmeasured_for(prompt_id, model_id)


def jinja_env(templates_dir: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(templates_dir),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["sparkline"] = sparkline
    env.filters["manifest_unmeasured"] = _unmeasured_lookup
    env.globals["viridis_color"] = viridis_color
    env.globals["heatmap_cell_style"] = heatmap_cell_style
    env.globals["OKABE_ITO"] = OKABE_ITO
    return env


def render_page(
    env: Environment,
    template_name: str,
    out_path: Path,
    context: dict,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = env.get_template(template_name).render(**context)
    # Preserve trailing newline for cleaner diffs.
    if not html.endswith("\n"):
        html += "\n"
    out_path.write_text(html)


def copy_static(static_dir: Path, out_dir: Path) -> None:
    dest = out_dir / "static"
    if dest.exists():
        shutil.rmtree(dest)
    if not static_dir.exists():
        return
    shutil.copytree(
        static_dir,
        dest,
        # README.md files inside /static/ are repo-side provenance notes
        # (see e.g. static/fonts/README.md for font licenses); they don't
        # belong in the served output.
        ignore=shutil.ignore_patterns(".gitkeep", "README.md"),
    )


@dataclass(frozen=True)
class Report:
    slug: str
    kind: str            # "weekly" or "monthly"
    title: str
    summary: str
    date: str            # ISO 8601 date
    week_id: str | None
    axes: tuple[str, ...]
    body: Markup         # already sanitized; safe to render without | safe

    @property
    def kind_label(self) -> str:
        return self.kind.capitalize()

    @property
    def canonical_path(self) -> str:
        return f"/reports/{self.slug}/"

    @property
    def canonical_url(self) -> str:
        return SITE_ORIGIN + self.canonical_path

    @property
    def year(self) -> int:
        return int(self.date[:4])

    @property
    def month(self) -> int:
        return int(self.date[5:7])

    @property
    def day(self) -> int:
        return int(self.date[8:10])

    @property
    def month_name(self) -> str:
        return MONTH_NAMES[self.month - 1]

    @property
    def updated_rfc3339(self) -> str:
        return f"{self.date}T00:00:00Z"


def _meta_scalar(meta: dict[str, list[str]], key: str, default: str = "") -> str:
    v = meta.get(key.lower(), [])
    return html_lib.unescape(v[0].strip()) if v else default


def _meta_list(meta: dict[str, list[str]], key: str) -> tuple[str, ...]:
    raw = _meta_scalar(meta, key)
    if not raw:
        return ()
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _sanitize_body(html: str) -> Markup:
    cleaned = nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_URL_SCHEMES,
        link_rel="noopener",
    )
    return Markup(cleaned)


def load_reports(content_dir: Path) -> list[Report]:
    if not content_dir.exists():
        return []
    reports: list[Report] = []
    for md_file in sorted(content_dir.glob("*.md")):
        md = markdown.Markdown(
            extensions=["meta", "tables", "sane_lists", "smarty"],
            output_format="html",
        )
        rendered = md.convert(md_file.read_text())
        meta: dict[str, list[str]] = getattr(md, "Meta", {})
        title = _meta_scalar(meta, "title")
        if not title:
            raise SystemExit(f"report {md_file.name} missing Title metadata")
        reports.append(
            Report(
                slug=md_file.stem,
                kind=_meta_scalar(meta, "type", "weekly"),
                title=title,
                summary=_meta_scalar(meta, "summary"),
                date=_meta_scalar(meta, "date"),
                week_id=_meta_scalar(meta, "week") or None,
                axes=_meta_list(meta, "axes"),
                body=_sanitize_body(rendered),
            )
        )
    reports.sort(key=lambda r: r.date, reverse=True)
    return reports


def render_reports(
    env: Environment, out_dir: Path, reports: list[Report], base_context: dict
) -> None:
    for r in reports:
        ctx = dict(base_context, report=r, og_slug=f"report-{r.slug}")
        render_page(
            env, "report.html", out_dir / "reports" / r.slug / "index.html", ctx
        )

    reports_by_year: dict[int, list[Report]] = {}
    for r in reports:
        reports_by_year.setdefault(r.year, []).append(r)
    for rs in reports_by_year.values():
        rs.sort(key=lambda r: r.date, reverse=True)

    idx_ctx = dict(base_context, reports_by_year=reports_by_year)
    render_page(env, "reports_index.html", out_dir / "reports" / "index.html", idx_ctx)


def write_atom_feed(
    env: Environment, out_dir: Path, reports: list[Report], base_context: dict
) -> None:
    feed_updated = (
        reports[0].updated_rfc3339
        if reports
        else base_context["build"]["built_at"]
    )
    ctx = dict(
        base_context,
        reports=reports,
        site_origin=SITE_ORIGIN,
        feed_updated=feed_updated,
    )
    # Use a .xml template to get xml-safe autoescaping.
    (out_dir / "feed.xml").write_text(
        env.get_template("feed.xml").render(**ctx)
    )


def render_dashboard(
    env: Environment, out_dir: Path, manifest: Manifest, base_context: dict
) -> None:
    weeks = manifest.all_weeks  # oldest-first
    # Two different lists, and pages need both. ``weeks`` is the calendar
    # axis for every table's columns and includes weeks the audit never
    # ran, so a lost week keeps its column. ``observed_weeks`` is what we
    # actually captured, and it is the only honest thing to count in a
    # sentence like "observed over N weeks": the 2026-W32 build said 16
    # when the 2026-W30 and 2026-W31 runs never started.
    observed_weeks = manifest.observed_weeks
    axes = sorted({p.axis for p in manifest.prompts})

    # ---- per-model pages + index ----
    for m in manifest.models:
        ctx = dict(
            base_context,
            model=m,
            weeks=weeks,
            observed_weeks=observed_weeks,
            axes=axes,
            prompts=manifest.prompts,
            manifest=manifest,
            timeseries=manifest.timeseries,
            og_slug=f"model-{m.model_id}",
        )
        render_page(
            env, "model.html",
            out_dir / "models" / m.model_id / "index.html", ctx,
        )
        # Model x week drill-down: render the current week and every
        # historical week the manifest carries. URL stability is a hard
        # project guarantee — once /models/<id>/<week>/ has been
        # published, it must continue to resolve. Without this loop the
        # link-rot guard fails every week as the current week rotates.
        render_page(
            env, "model_week.html",
            out_dir / "models" / m.model_id / manifest.snapshot.week_id / "index.html",
            dict(ctx, week_id=manifest.snapshot.week_id,
                 week_metrics=[mx for mx in manifest.metrics if mx.model_id == m.model_id]),
        )
        for h in manifest.history:
            # Render the page even when the model has no metrics that
            # week — off-cadence weeks produce empty pages, but the URL
            # itself must stay resolvable. The template handles
            # empty week_metrics by showing a "no data this week" state.
            week_metrics = [mx for mx in h.metrics if mx.model_id == m.model_id]
            render_page(
                env, "model_week.html",
                out_dir / "models" / m.model_id / h.week_id / "index.html",
                dict(ctx, week_id=h.week_id, week_metrics=week_metrics),
            )

    render_page(
        env, "models_index.html",
        out_dir / "models" / "index.html",
        dict(base_context, models=manifest.models, manifest=manifest),
    )

    # ---- per-axis pages + index ----
    for axis in axes:
        axis_prompts = [p for p in manifest.prompts if p.axis == axis]
        ctx = dict(
            base_context,
            axis=axis,
            models=manifest.models,
            weeks=weeks,
            observed_weeks=observed_weeks,
            axis_prompts=axis_prompts,
            table=axis_metric_table(manifest, axis),
            manifest=manifest,
            timeseries=manifest.timeseries,
            og_slug=f"axis-{axis}",
        )
        render_page(
            env, "axis.html",
            out_dir / "axes" / axis / "index.html", ctx,
        )

    render_page(
        env, "axes_index.html",
        out_dir / "axes" / "index.html",
        dict(base_context, axes=axes, manifest=manifest),
    )

    # ---- per-prompt pages + index ----
    # Sample responses behind this week's numbers, keyed (prompt, model).
    # Restricted to the public corpus: the snapshot is the one input on
    # this path that could carry held-out prompts, and they must never
    # reach a rendered page.
    week_excerpts = excerpts.load_for_week(
        manifest.snapshot.week_id,
        REPO_ROOT / "data" / "snapshots" / manifest.snapshot.week_id
        / "responses.jsonl.gz",
        prompt_ids={p.prompt_id for p in manifest.prompts},
    )
    for p in manifest.prompts:
        ctx = dict(
            base_context,
            prompt=p,
            models=manifest.models,
            weeks=weeks,
            observed_weeks=observed_weeks,
            manifest=manifest,
            timeseries=manifest.timeseries,
            excerpts_for=lambda mid, _pid=p.prompt_id: week_excerpts.get((_pid, mid)),
            max_excerpt_chars=excerpts.MAX_EXCERPT_CHARS,
            og_slug=f"prompt-{p.prompt_id}",
        )
        render_page(
            env, "prompt.html",
            out_dir / "prompts" / p.prompt_id / "index.html", ctx,
        )

    render_page(
        env, "prompts_index.html",
        out_dir / "prompts" / "index.html",
        dict(base_context, prompts=manifest.prompts, manifest=manifest),
    )


_METRICS_COLUMNS = [
    "week_id", "prompt_id", "model_id", "n_samples", "unusable_samples",
    "refusal_rate", "refusal_ci_lower", "refusal_ci_upper",
    "hedge_density", "length_median", "length_p25", "length_p75",
    "stance", "stance_confidence", "embedding_centroid_shift",
    "flagged_for_review", "flag_reason",
]


def _per_week_readme(week_id: str) -> str:
    return (
        f"# Meridian snapshot: {week_id}\n\n"
        f"ISO week {week_id}. Contains computed metrics plus the raw\n"
        f"response samples they were derived from.\n\n"
        f"## Files\n\n"
        f"- `metrics.csv` / `metrics.jsonl` — one row per (prompt × model).\n"
        f"- `metrics.parquet` — same data, columnar; present when the site\n"
        f"  builder had `pyarrow` installed.\n"
        f"- `manifest.json` — present on the current-week snapshot only;\n"
        f"  the full schema-validated manifest the site renders from.\n"
        f"- `responses.jsonl.gz` — gzipped stream of raw\n"
        f"  :class:`meridian.runners.base.Sample` records for every\n"
        f"  public prompt captured this week. Held-out prompt responses\n"
        f"  are excluded. Present when the pipeline emitted one;\n"
        f"  `SHA256SUMS` lists it when it is.\n"
        f"- `SHA256SUMS` — hex digests for integrity verification.\n\n"
        f"## License\n\n"
        f"CC-BY-SA 4.0. See /data/schema/ for full column definitions.\n"
        f"Citation: https://meridianaudit.org/data/{week_id}/\n"
    )


#: Filename for a gap week's only artifact. Deliberately not
#: ``README.md``: the snapshot index at /data/ lists each week's files
#: by name, so this is what makes a gap row legible as a gap in the
#: index itself rather than only on the week's own page.
GAP_WEEK_NOTICE = "NO-DATA.md"


def _gap_week_readme(week_id: str) -> str:
    """Notice file for an ISO week in which the audit captured nothing.

    Deliberately blunt. Someone who lands here from a citation or from
    the snapshot index needs to know within one line that there is no
    data for this week and that there never will be, not to go hunting
    for a file that was never written.
    """
    return (
        f"# Meridian gap week: {week_id}\n\n"
        f"ISO week {week_id} has no data. The scheduled weekly run did\n"
        f"not produce samples for any model, so there are no metrics,\n"
        f"no responses, and no manifest for this week.\n\n"
        f"## Why this directory exists\n\n"
        f"A longitudinal record whose holes are invisible is worse than\n"
        f"one with holes. This week sits between weeks that do have\n"
        f"data, so it keeps a directory, a URL, and a column in every\n"
        f"model-by-week table on the site. Charts break the line here\n"
        f"rather than joining the weeks on either side.\n\n"
        f"## Not backfilled\n\n"
        f"The week was not re-sampled later. A run made after the fact\n"
        f"would carry today's model behaviour under this week's label,\n"
        f"which is precisely the substitution this project exists to\n"
        f"make visible.\n\n"
        f"The cause of this specific gap is documented at\n"
        f"https://meridianaudit.org/methodology/#data-gaps\n\n"
        f"## License\n\n"
        f"CC-BY-SA 4.0.\n"
    )


def _publish_gap_weeks(
    env: Environment,
    out_dir: Path,
    manifest: Manifest,
    base_context: dict,
) -> list[dict]:
    """Emit a ``/data/{week}/`` landing page for every week with no snapshot.

    Written after the 2026-W30 and 2026-W31 outage, when ``/data/``
    listed 2026-W29 immediately above 2026-W32 and nothing on the page
    said that two weeks were missing. A reader scanning the index had no
    way to distinguish "the audit ran every week" from "the audit lost
    two weeks", which is a methodology problem rather than a cosmetic
    one.

    Each gap week gets a real directory so the index row's links
    resolve: a README stating there is no data, and a SHA256SUMS over
    it. Row count is 0 and the entry is flagged ``is_gap`` so the
    snapshot index can label it.

    A week is only published as a gap when the committed record agrees
    that it is one. ``missing_weeks`` infers "the audit captured nothing
    and never will" from the week's absence from *this manifest's*
    history, and that inference is not always sound: the manifest
    writer's backfill loop skips a prior manifest it cannot parse, and
    skips any week left with no metrics after prompt-id scoping, so a
    corpus prompt rename or one unreadable file is enough to drop a week
    that does have a snapshot. Publishing a NO-DATA notice over a week
    whose data is sitting in ``data/`` would assert a falsehood about
    the public record and quietly replace that week's metrics and
    responses with a stub. Both trees are read-only here.
    """
    entries: list[dict] = []
    for week_id in manifest.missing_weeks:
        committed = [
            p for p in (
                REPO_ROOT / "data" / "manifests" / f"{week_id}.json",
                REPO_ROOT / "data" / "snapshots" / week_id,
            )
            if p.exists()
        ]
        if committed:
            print(
                f"WARNING: {week_id} is absent from the manifest's history but "
                f"the committed record has {', '.join(str(p) for p in committed)}. "
                f"Refusing to publish a no-data notice over a week that has "
                f"data. The manifest is wrong, not the archive: check the "
                f"backfill in meridian/pipeline/manifest_writer.py.",
                file=sys.stderr,
            )
            continue
        snap_dir = out_dir / "data" / week_id
        snap_dir.mkdir(parents=True, exist_ok=True)
        notice_text = _gap_week_readme(week_id)
        (snap_dir / GAP_WEEK_NOTICE).write_text(notice_text)
        (snap_dir / "SHA256SUMS").write_text(
            f"{_sha256_of(notice_text)}  {GAP_WEEK_NOTICE}\n"
        )
        entry = {
            "week_id": week_id,
            "is_current": False,
            "is_gap": True,
            "files": [
                {
                    "name": GAP_WEEK_NOTICE,
                    "size": len(notice_text.encode("utf-8")),
                }
            ],
            "row_count": 0,
            "has_responses": False,
        }
        entries.append(entry)
        render_page(
            env, "data_gap.html",
            snap_dir / "index.html",
            dict(base_context, week=entry, manifest=manifest),
        )
    return entries


def _copy_responses_snapshot(week_id: str, snap_dir: Path) -> dict | None:
    """Copy ``data/snapshots/{week}/responses.jsonl.gz`` into the
    published snapshot dir.

    Returns ``{"size": int, "sha256": str}`` when the source file
    exists, else ``None``. Missing source is not an error — older weeks
    may predate the snapshot-emission feature, and tests that do not
    seed raw samples still need to build the site cleanly.
    """
    src = REPO_ROOT / "data" / "snapshots" / week_id / "responses.jsonl.gz"
    if not src.exists():
        return None
    content = src.read_bytes()
    dest = snap_dir / "responses.jsonl.gz"
    dest.write_bytes(content)
    return {
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _opt_float(value) -> float | None:
    """``float(value)``, or None when the value is absent.

    Used for the nullable length quantiles: a cell whose only usable
    samples were body-less refusals has no length distribution at all
    (see ``schema.LengthStats``), and every export has to carry that
    absence rather than coerce it to a number.
    """
    return None if value is None else float(value)


def _try_write_parquet(week_id: str, metrics: list, snap_dir: Path) -> int | None:
    """Best-effort Parquet emission alongside CSV/JSONL. Returns byte size
    if pyarrow is available and the write succeeded; ``None`` otherwise."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return None

    rows: list[dict] = []
    for m in metrics:
        rows.append({
            "week_id": week_id,
            "prompt_id": m.prompt_id,
            "model_id": m.model_id,
            "n_samples": m.n_samples,
            "unusable_samples": m.unusable_samples,
            "refusal_rate": float(m.refusal_rate),
            "refusal_ci_lower": float(m.refusal_ci.lower),
            "refusal_ci_upper": float(m.refusal_ci.upper),
            "hedge_density": _opt_float(m.hedge_density),
            # Null quantiles stay null in the export. A cell measured
            # entirely from body-less refusals has no length
            # distribution, and writing 0.0 would publish a run of
            # zero-word answers nobody wrote.
            "length_median": _opt_float(m.length.median),
            "length_p25": _opt_float(m.length.p25),
            "length_p75": _opt_float(m.length.p75),
            "stance": m.stance,
            "stance_confidence": (
                None if m.stance_confidence is None else float(m.stance_confidence)
            ),
            "embedding_centroid_shift": (
                None if m.embedding_centroid_shift is None
                else float(m.embedding_centroid_shift)
            ),
            "flagged_for_review": bool(m.flagged_for_review),
            "flag_reason": m.flag_reason,
        })
    table = pa.Table.from_pylist(rows)
    out = snap_dir / "metrics.parquet"
    pq.write_table(table, out)
    return out.stat().st_size


def _blank_if_none(value) -> object:
    """CSV rendering of a nullable numeric column."""
    return "" if value is None else value


def _metrics_to_csv(week_id: str, metrics: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(_METRICS_COLUMNS)
    for m in metrics:
        w.writerow([
            week_id, m.prompt_id, m.model_id, m.n_samples, m.unusable_samples,
            m.refusal_rate, m.refusal_ci.lower, m.refusal_ci.upper,
            _blank_if_none(m.hedge_density),
            # Empty, not 0, when there was no text to measure. Same
            # convention the optional columns below already use, and the
            # reason is stronger here: a 0 in a length column reads as a
            # measurement.
            _blank_if_none(m.length.median),
            _blank_if_none(m.length.p25),
            _blank_if_none(m.length.p75),
            m.stance, m.stance_confidence if m.stance_confidence is not None else "",
            m.embedding_centroid_shift if m.embedding_centroid_shift is not None else "",
            str(m.flagged_for_review).lower(),
            m.flag_reason or "",
        ])
    return buf.getvalue()


def _metrics_to_jsonl(week_id: str, metrics: list) -> str:
    lines = []
    for m in metrics:
        obj = m.model_dump()
        obj["week_id"] = week_id
        lines.append(json.dumps(obj, sort_keys=True, default=str))
    return "\n".join(lines) + ("\n" if lines else "")


def _sha256_of(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def publish_data(
    env: Environment,
    out_dir: Path,
    manifest_path: Path,
    manifest: Manifest,
    base_context: dict,
) -> None:
    """Publish per-week bulk data snapshots under /data/{week}/.

    Current week gets the full manifest.json. Every week gets metrics.csv,
    metrics.jsonl, README.md, and SHA256SUMS. A /data/ index page lists all.
    """
    snapshots_meta: list[dict] = []

    # Build a week -> metrics map (current + history).
    week_metrics: dict[str, list] = {
        h.week_id: list(h.metrics) for h in manifest.history
    }
    week_metrics[manifest.snapshot.week_id] = list(manifest.metrics)

    for week_id in sorted(week_metrics.keys()):
        metrics = week_metrics[week_id]
        snap_dir = out_dir / "data" / week_id
        snap_dir.mkdir(parents=True, exist_ok=True)

        csv_text = _metrics_to_csv(week_id, metrics)
        jsonl_text = _metrics_to_jsonl(week_id, metrics)
        readme_text = _per_week_readme(week_id)

        artifacts = {
            "metrics.csv": csv_text,
            "metrics.jsonl": jsonl_text,
            "README.md": readme_text,
        }
        if week_id == manifest.snapshot.week_id:
            artifacts["manifest.json"] = manifest_path.read_text()

        sums = []
        for name, text in artifacts.items():
            (snap_dir / name).write_text(text)
            sums.append(f"{_sha256_of(text)}  {name}")

        # Optional Parquet companion: emitted when pyarrow is installed.
        parquet_bytes = _try_write_parquet(week_id, metrics, snap_dir)
        if parquet_bytes is not None:
            pq_content = (snap_dir / "metrics.parquet").read_bytes()
            pq_sha = hashlib.sha256(pq_content).hexdigest()
            sums.append(f"{pq_sha}  metrics.parquet")

        responses_meta = _copy_responses_snapshot(week_id, snap_dir)
        if responses_meta is not None:
            sums.append(f"{responses_meta['sha256']}  responses.jsonl.gz")

        (snap_dir / "SHA256SUMS").write_text("\n".join(sorted(sums)) + "\n")

        files_meta = [
            {"name": name, "size": len(text.encode("utf-8"))}
            for name, text in artifacts.items()
        ]
        if parquet_bytes is not None:
            files_meta.append({"name": "metrics.parquet", "size": parquet_bytes})
        if responses_meta is not None:
            files_meta.append(
                {"name": "responses.jsonl.gz", "size": responses_meta["size"]}
            )
        week_entry = {
            "week_id": week_id,
            "is_current": week_id == manifest.snapshot.week_id,
            "is_gap": False,
            "files": files_meta,
            "row_count": len(metrics),
            "has_responses": responses_meta is not None,
        }
        snapshots_meta.append(week_entry)

        # Per-week landing page so /data/{week}/ resolves instead of 404ing
        # as a bare directory (the /data/ index links it). Because the page
        # is an index.html, collect_urls() registers /data/{week}/ in
        # urls.txt, so the link-rot guard tracks it going forward. Mirrors
        # the /models/{id}/{week}/ pattern in render_dashboard; both rely on
        # the manifest carrying full history so every week is re-emitted.
        render_page(
            env, "data_week.html",
            snap_dir / "index.html",
            dict(base_context, week=week_entry, manifest=manifest),
        )

    # Weeks the audit never ran get a directory too, so the index below
    # shows the hole in the record instead of listing 2026-W29 directly
    # above 2026-W32.
    snapshots_meta.extend(_publish_gap_weeks(env, out_dir, manifest, base_context))

    # Data index page.
    render_page(
        env, "data_index.html",
        out_dir / "data" / "index.html",
        dict(base_context, snapshots=sorted(snapshots_meta, key=lambda s: s["week_id"], reverse=True), manifest=manifest),
    )

    # Schema documentation page.
    render_page(
        env, "data_schema.html",
        out_dir / "data" / "schema" / "index.html",
        dict(base_context, columns=_METRICS_COLUMNS, manifest=manifest),
    )


def write_search_index(
    out_dir: Path, manifest: Manifest, reports: list
) -> None:
    """Build the client-side search index.

    Tiny custom format: a list of {kind, id, title, url, text} records. The
    accompanying JS (search.js) does naive substring/word-prefix matching
    against this index. Keeping the format custom avoids pulling in a
    third-party index tool and keeps the JSON compact.
    """
    records: list[dict] = []
    for p in manifest.prompts:
        records.append({
            "kind": "prompt",
            "id": p.prompt_id,
            "title": p.title,
            "url": f"/prompts/{p.prompt_id}/",
            "text": f"{p.title} {p.axis} {p.description or ''}".strip(),
        })
    for m in manifest.models:
        records.append({
            "kind": "model",
            "id": m.model_id,
            "title": m.display_name,
            "url": f"/models/{m.model_id}/",
            "text": f"{m.display_name} {m.provider} {m.version_string}",
        })
    for r in reports:
        records.append({
            "kind": "report",
            "id": r.slug,
            "title": r.title,
            "url": r.canonical_path,
            "text": r.summary,
        })
    out = out_dir / "static" / "search-index.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, separators=(",", ":")))


def write_redirects(
    env: Environment,
    redirects_path: Path,
    out_dir: Path,
    base_context: dict,
) -> int:
    """Emit a redirect HTML page for every entry in ``site/redirects.yaml``.

    The emitted pages participate in ``urls.txt`` (and therefore in the
    link-rot guard) just like any other page — the whole point is that a
    moved URL remains served rather than 404-ing. Returns the number of
    redirects written, mostly for the build summary.
    """
    if not redirects_path.exists():
        return 0
    raw = yaml.safe_load(redirects_path.read_text()) or {}
    entries = raw.get("redirects") or []
    written = 0
    for entry in entries:
        src = entry["from"].strip()
        dst = entry["to"].strip()
        if not src.startswith("/") or not dst.startswith("/"):
            raise SystemExit(
                f"redirects.yaml: from/to must be site-root-relative paths; "
                f"got from={src!r} to={dst!r}"
            )
        if src == dst:
            raise SystemExit(
                f"redirects.yaml: self-redirect on {src!r}; remove or fix"
            )
        reason = entry.get("reason")

        # Canonicalize the output path: /foo/ -> foo/index.html; /foo.html -> foo.html.
        rel = src[1:]
        if rel.endswith("/") or rel == "":
            out_path = out_dir / (rel + "index.html")
        else:
            out_path = out_dir / rel
            if not out_path.suffix:
                out_path = out_path / "index.html"
        ctx = dict(base_context, from_path=src, to=dst, reason=reason)
        render_page(env, "redirect.html", out_path, ctx)
        written += 1
    return written


def write_robots(out_dir: Path) -> None:
    (out_dir / "robots.txt").write_text(
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {SITE_ORIGIN}/sitemap.xml\n"
    )


def write_cname(out_dir: Path) -> None:
    """Emit `dist/CNAME` so GitHub Pages keeps the custom-domain
    setting across deploys. Without this file, Pages strips the custom
    domain on every successful deploy and the site falls back to
    `<user>.github.io/<repo>/`. Host is derived from SITE_ORIGIN so the
    two can never drift apart."""
    from urllib.parse import urlparse

    host = urlparse(SITE_ORIGIN).netloc
    (out_dir / "CNAME").write_text(host + "\n")


def write_humans(out_dir: Path, build_meta: dict) -> None:
    lines = [
        "/* TEAM */",
        "Project: Meridian",
        "Site: https://meridianaudit.org/",
        "Source: https://github.com/digital-grease/meridian",
        "",
        "/* SITE */",
        f"Built: {build_meta['built_at']}",
        f"Snapshot: {build_meta['manifest_week']}",
        f"Site commit: {build_meta['git_sha']}",
        "Language: English",
        "Standards: HTML5, CSS3",
        "Components: None (no third-party resources)",
        "",
    ]
    (out_dir / "humans.txt").write_text("\n".join(lines))


def write_sitemap(out_dir: Path) -> None:
    urls = collect_urls(out_dir)
    entries = []
    for u in sorted(urls):
        if u.endswith(".xml") or u.endswith(".txt"):
            continue
        if u == "/404.html":
            continue
        entries.append(
            f"  <url><loc>{SITE_ORIGIN}{u}</loc></url>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )
    (out_dir / "sitemap.xml").write_text(xml)


def collect_urls(out_dir: Path) -> set[str]:
    """All public URL paths the build produced, as site-root-relative strings."""
    urls: set[str] = set()
    for f in out_dir.rglob("*.html"):
        rel = f.relative_to(out_dir).as_posix()
        # /foo/index.html canonicalizes to /foo/
        if rel.endswith("/index.html"):
            urls.add("/" + rel[: -len("index.html")])
        elif rel == "index.html":
            urls.add("/")
        else:
            urls.add("/" + rel)
    # Also track the data/feed-like files that journalists will cite.
    for name in ("feed.xml", "sitemap.xml", "robots.txt"):
        if (out_dir / name).exists():
            urls.add("/" + name)
    return urls


def load_run_log_summary(repo_root: Path) -> list:
    """Return WeeklySummary rows for the internal health page.

    Graceful when the log doesn't exist yet (empty list).

    ``MERIDIAN_RUN_LOG`` overrides the path. That exists so a test can
    script this page's input without touching the real log: on
    2026-08-15 the health-page tests, which swapped
    ``data/run_log.jsonl`` in place and restored it in a finally block,
    were run concurrently by two processes. One captured the other's
    scripted file as "the original" and restored that, and 15 of the 17
    real entries were destroyed. They were recoverable from git, but the
    run log is the append-only public record and retention is forever,
    so no test may write to it at all. Point the override at a tmp_path
    instead.
    """
    from meridian.pipeline.run_log import read_run_log
    from meridian.pipeline.run_log_summary import summarize_weekly

    override = os.environ.get("MERIDIAN_RUN_LOG")
    log_path = Path(override) if override else repo_root / "data" / "run_log.jsonl"
    return summarize_weekly(read_run_log(log_path))


# Reference scales for normalising per-metric weekly deltas into a
# unitless "drift signal" for the home-page heatmap. These are not
# meant to be statistically tight — they're "what counts as a big
# week-over-week move" so the colour ramp tracks intuition. Tightened
# scales are fine; the heatmap colour is normalised against
# `max_score_in_grid` at render time so absolute scale doesn't matter.
_DRIFT_SCALES = {
    "refusal_rate": 1.0,        # full Bernoulli range
    "hedge_density": 5.0,        # typical 0-10 markers/100 tok
    "length_median": None,       # uses relative scale at compute time
}

# Headline metric for each axis's model x week table.
#
# The table used to hardcode refusal_rate, but refusal_rate only
# carries signal on refusal-boundary. Measured across the W17-W28
# corpus, the spread of per-(model, week) axis means was *exactly*
# 0.00 on factual-stability, historical-contested, neutral-control and
# scientific-consensus, and 0.03 on political: five of six axis pages
# rendered a wall of true-but-vacuous zeros. Each axis now leads with
# the metric that actually moves on it:
#
#   refusal-boundary      refusal_rate:  what the model declines is
#                                          the whole point of the axis
#   political             hedge_density: stance-bearing axes drift in
#   historical-contested                   framing, not refusal; hedge
#   scientific-consensus                   density is CLAUDE.md's
#                                          framing measure
#   neutral-control       length_median: these axes exist to detect
#   factual-stability                      "the model itself changed";
#                                          length distribution shift is
#                                          the canonical silent-update
#                                          detector
#
# Axes not listed fall back to refusal_rate. Adding an axis to the
# corpus therefore never breaks the build, it just gets the default
# until someone picks a better metric for it.
_AXIS_HEADLINE_METRIC = {
    "refusal-boundary": "refusal_rate",
    "political": "hedge_density",
    "historical-contested": "hedge_density",
    "scientific-consensus": "hedge_density",
    "neutral-control": "length_median",
    "factual-stability": "length_median",
}
_DEFAULT_AXIS_METRIC = "refusal_rate"

# Presentation for each renderable metric. `fixed_max` pins the colour
# ramp's top; None means "scale to the largest cell in this table".
#
# Only refusal_rate gets a fixed ceiling: it is a probability, so 0.80
# means 80% of samples refused regardless of which axis you are looking
# at, and pinning it to 1.0 keeps that reading comparable across pages.
# hedge_density and length_median have no comparable natural ceiling
# (observed hedge_density tops out near 1.1 against a _DRIFT_SCALES
# reference of 5.0, which would flatten the whole table into the dark
# end of the ramp), so they scale to the data.
#
# Every ramp is anchored at zero rather than at the table's minimum.
# Min-max normalisation would stretch a trivial spread across the full
# colour range and invent visual drift where there is none, which is
# the exact failure this project exists not to commit.
_METRIC_META = {
    "refusal_rate": {
        "label": "Refusal rate",
        "fmt": "%.2f",
        "fixed_max": 1.0,
        "blurb": "Fraction of samples that declined to answer, "
                 "averaged across the prompts in this axis.",
    },
    "hedge_density": {
        "label": "Hedge density",
        "fmt": "%.2f",
        "fixed_max": None,
        "blurb": "Hedging markers (“it’s important to note”, "
                 "“some argue”) per 100 tokens, averaged across the "
                 "prompts in this axis. A framing measure, not a "
                 "refusal measure.",
    },
    "length_median": {
        "label": "Median response length",
        "fmt": "%.0f",
        "fixed_max": None,
        "blurb": "Median response length in tokens, averaged across the "
                 "prompts in this axis. Sustained shifts often "
                 "accompany a model update.",
    },
}


def axis_metric_table(manifest: Manifest, axis: str) -> dict:
    """Build the per-axis model x week table of that axis's headline
    metric.

    Returns a dict with:
      * ``metric`` / ``meta``: the chosen metric id and its presentation
      * ``weeks``: column order (oldest-first)
      * ``rows``: one per model, ``{"model": Model, "cells": [...]}``
        where each cell is a float mean or ``None`` for "not sampled
        that week"
      * ``max``: top of the colour ramp (ramp is anchored at 0.0)
      * ``has_gaps``: True if any cell is None, so the template only
        explains the gap marker on tables that actually have one
      * ``missing_weeks``: the subset of ``weeks`` in which the audit
        ran nothing at all, so those columns can be labelled as an
        outage rather than as a cadence skip

    A None cell means the model produced no samples that week, most
    often because frontier models alternate on a biweekly cadence.
    That is emphatically not a measurement of zero, and the two must
    never render alike.

    Since 2026-W30 ``weeks`` also carries weeks with no snapshot at all
    (see ``Manifest.missing_weeks``). Every row is None in such a
    column, which is correct: no model was sampled. Gap sentinels
    arriving from ``timeseries`` are dropped here rather than averaged,
    keeping the "aggregate statistics exclude the missing cell rather
    than imputing it" promise on /methodology/.
    """
    metric = _AXIS_HEADLINE_METRIC.get(axis, _DEFAULT_AXIS_METRIC)
    meta = _METRIC_META[metric]
    prompts = [p for p in manifest.prompts if p.axis == axis]
    weeks = manifest.all_weeks

    rows = []
    observed: list[float] = []
    has_gaps = False
    for model in manifest.models:
        by_week: dict[str, list[float]] = {}
        for p in prompts:
            for week_id, value in manifest.timeseries(p.prompt_id, model.model_id, metric):
                if not is_measured(value):
                    continue
                by_week.setdefault(week_id, []).append(value)
        cells: list[float | None] = []
        for w in weeks:
            vals = by_week.get(w)
            if vals:
                mean = sum(vals) / len(vals)
                observed.append(mean)
                cells.append(mean)
            else:
                has_gaps = True
                cells.append(None)
        rows.append({"model": model, "cells": cells})

    top = meta["fixed_max"]
    if top is None:
        top = max(observed) if observed else 0.0
    if not top or top <= 0:
        # Every cell is zero or absent. Any positive ramp top renders
        # them all at the ramp's floor, which is the honest picture.
        top = 1.0
    return {
        "metric": metric, "meta": meta, "weeks": weeks,
        "rows": rows, "max": top, "has_gaps": has_gaps,
        "missing_weeks": manifest.missing_weeks,
    }


def _avg(records, attr):
    vals = []
    for r in records:
        v = (
            getattr(r, attr, None)
            if "." not in attr
            else getattr(r.length, attr.split(".")[1])
        )
        if v is not None:
            vals.append(float(v))
    return sum(vals) / len(vals) if vals else None


def _metrics_for_model_at_week(
    manifest: Manifest,
    model_id: str,
    week_id: str,
    axis_prompt_ids: set[str],
):
    """Return the (axis-scoped) metric records for a model at a given
    week, looking in current first and history second."""
    if week_id == manifest.snapshot.week_id:
        return [
            m for m in manifest.metrics
            if m.model_id == model_id and m.prompt_id in axis_prompt_ids
        ]
    for h in manifest.history:
        if h.week_id == week_id:
            return [
                m for m in h.metrics
                if m.model_id == model_id and m.prompt_id in axis_prompt_ids
            ]
    return []


def _latest_week_with_data(manifest: Manifest, model_id: str, axis_prompt_ids: set[str]) -> str | None:
    """Walk current-then-history and return the most recent week_id
    where ``model_id`` has any metric records on prompts in this axis."""
    if any(
        m.model_id == model_id and m.prompt_id in axis_prompt_ids
        for m in manifest.metrics
    ):
        return manifest.snapshot.week_id
    for h in reversed(manifest.history):  # newest first
        if any(
            m.model_id == model_id and m.prompt_id in axis_prompt_ids
            for m in h.metrics
        ):
            return h.week_id
    return None


def _drift_score_for_axis(
    *,
    axis: str,
    model_id: str,
    manifest: Manifest,
) -> dict | None:
    """Compute one heatmap cell: the dominant week-over-week shift on
    this (axis, model). Returns ``None`` when this (axis, model) has
    no measurement in either current or history.

    Modes:
      * ``"delta"``: two consecutive weeks of data → cell shows
        |as_of - prior| normalised.
      * ``"absolute"``: only one week of data → cell shows the
        as-of-week absolute value.

    For cadence-skipped frontier models (Opus on odd weeks, GPT-5.1 on
    even), the as-of week is the most recent week the model was sampled
    on this axis, NOT the manifest's snapshot week. The cell carries an
    ``as_of_week`` field so the template can flag stale measurements.
    """
    axis_prompt_ids = {p.prompt_id for p in manifest.prompts if p.axis == axis}
    if not axis_prompt_ids:
        return None
    as_of_week = _latest_week_with_data(manifest, model_id, axis_prompt_ids)
    if as_of_week is None:
        return None
    as_of_metrics = _metrics_for_model_at_week(manifest, model_id, as_of_week, axis_prompt_ids)
    if not as_of_metrics:
        return None

    cur = {
        "refusal_rate": _avg(as_of_metrics, "refusal_rate"),
        "hedge_density": _avg(as_of_metrics, "hedge_density"),
        "length_median": _avg(as_of_metrics, "length.median"),
    }

    # Find the most recent week strictly older than as_of_week with
    # data for this model on this axis.
    prior_metrics: list = []
    prior_week: str | None = None
    candidates = [(h.week_id, h.metrics) for h in manifest.history]
    if as_of_week != manifest.snapshot.week_id:
        candidates.append((manifest.snapshot.week_id, manifest.metrics))
    candidates = [(w, ms) for (w, ms) in candidates if w < as_of_week]
    candidates.sort(key=lambda t: t[0], reverse=True)
    for w, ms in candidates:
        cand = [m for m in ms if m.model_id == model_id and m.prompt_id in axis_prompt_ids]
        if cand:
            prior_metrics = cand
            prior_week = w
            break

    if not prior_metrics:
        score = max(
            (cur["refusal_rate"] or 0.0) / _DRIFT_SCALES["refusal_rate"],
            (cur["hedge_density"] or 0.0) / _DRIFT_SCALES["hedge_density"],
        )
        return {
            "axis": axis, "model_id": model_id,
            "score": round(score, 3), "mode": "absolute",
            "metric": "absolute snapshot",
            "current": cur, "prior": None,
            "as_of_week": as_of_week, "prior_week": None,
        }

    prior = {
        "refusal_rate": _avg(prior_metrics, "refusal_rate"),
        "hedge_density": _avg(prior_metrics, "hedge_density"),
        "length_median": _avg(prior_metrics, "length.median"),
    }
    deltas: dict[str, float] = {}
    for k in ("refusal_rate", "hedge_density"):
        c, p = cur[k], prior[k]
        if c is not None and p is not None:
            deltas[k] = abs(c - p) / _DRIFT_SCALES[k]
    if cur["length_median"] is not None and prior["length_median"]:
        denom = max(prior["length_median"], cur["length_median"]) / 2
        deltas["length_median"] = abs(cur["length_median"] - prior["length_median"]) / denom if denom else 0.0
    if not deltas:
        return None
    dominant_metric = max(deltas, key=deltas.get)
    return {
        "axis": axis, "model_id": model_id,
        "score": round(deltas[dominant_metric], 3),
        "mode": "delta",
        "metric": dominant_metric,
        "current": cur, "prior": prior,
        "as_of_week": as_of_week, "prior_week": prior_week,
    }


def drift_heatmap(manifest: Manifest) -> dict:
    """Compute the home-page drift heatmap data.

    The grid is "latest measurement per (axis × model)", not
    "current week only". Cadence-alternated frontier models (Opus on
    even weeks, GPT-5.1 on odd) would otherwise leave half the grid
    empty every week — which would imply nothing about the model's
    drift, only about the week's roster. Each cell carries an
    ``as_of_week`` field so the template can flag rows whose data is
    older than the manifest's snapshot week.

    Returns a dict with:
      * ``axes``: ordered list of axis ids
      * ``models``: ordered list of model_ids — both currently-active
        and carry-forward
      * ``cells``: dict[(axis, model_id)] → cell dict; missing pairs
        are absent
      * ``max_score``: the largest score in the grid (for colour
        scaling); falls back to 1.0 if the grid is empty
      * ``mode``: "delta" if any cell uses delta mode, else
        "absolute" — drives the caption
      * ``has_stale``: True if any cell's as_of_week is older than the
        snapshot week (drives the "some columns show last-seen data"
        caption note)
    """
    axes = sorted({p.axis for p in manifest.prompts})
    # Active models first, then carry-forward, so the grid puts
    # current-week data on the left.
    active = [m.model_id for m in manifest.models if m.available]
    inactive = [m.model_id for m in manifest.models if not m.available]
    models = active + inactive
    cells: dict[tuple[str, str], dict] = {}
    for axis in axes:
        for model_id in models:
            cell = _drift_score_for_axis(
                axis=axis, model_id=model_id, manifest=manifest,
            )
            if cell is not None:
                cells[(axis, model_id)] = cell
    if not cells:
        return {
            "axes": axes, "models": models,
            "cells": {}, "max_score": 1.0, "row_maxes": {},
            "mode": "absolute", "has_stale": False,
        }
    max_score = max(c["score"] for c in cells.values()) or 1.0
    # Per-axis (per-row) max for color normalization. The homepage
    # heatmap colors each cell relative to its row's largest score so
    # rows with quiet weeks (factual-stability, neutral-control) still
    # show within-row spread instead of collapsing to one color when
    # one row's outlier (a refusal-boundary spike) dominates a global
    # max. Cross-row color comparison loses meaning — readers compare
    # numbers for that. Caption explains.
    row_maxes: dict[str, float] = {}
    for axis in axes:
        row_scores = [
            c["score"] for (a, _m), c in cells.items() if a == axis
        ]
        row_maxes[axis] = max(row_scores) if row_scores else 0.0
    mode = "delta" if any(c["mode"] == "delta" for c in cells.values()) else "absolute"
    has_stale = any(
        c["as_of_week"] != manifest.snapshot.week_id for c in cells.values()
    )
    return {
        "axes": axes, "models": models,
        "cells": cells, "max_score": max_score, "row_maxes": row_maxes,
        "mode": mode, "has_stale": has_stale,
    }


def model_refusal_series(manifest: Manifest) -> dict[str, list[float]]:
    """Per-model time series of mean refusal_rate across all current
    prompts for that model, oldest-first.

    Used to inject a sparkline into each model tile on the home page.
    Models with no current-week data return an empty list and the
    template can show a "no data" affordance.
    """
    out: dict[str, list[float]] = {}
    weeks: list[tuple[str, list]] = []
    for h in manifest.history:
        weeks.append((h.week_id, h.metrics))
    weeks.append((manifest.snapshot.week_id, manifest.metrics))

    for model in manifest.models:
        series: list[float] = []
        for _wk, metrics in weeks:
            vals = [m.refusal_rate for m in metrics if m.model_id == model.model_id]
            if vals:
                series.append(round(sum(vals) / len(vals), 3))
        out[model.model_id] = series
    return out


def notable_shifts(manifest: Manifest, *, top_n: int = 3) -> list[dict]:
    """Top-N largest week-over-week metric shifts across the manifest.

    Each result is a single (prompt × model × metric) shift expressed
    as the most recent paired-week delta normalized to its reference
    scale (matching :func:`drift_heatmap`'s normalisation). Used by the
    home-page callout cards to surface "this week's headline drifts"
    without requiring the reader to scan the whole heatmap.

    Returns an empty list when no prior week is available (the caller
    should render an empty-state section).
    """
    if not manifest.history:
        return []
    # Most-recent prior value PER (prompt × model), walking newest→oldest.
    # We must NOT stop at the first non-empty snapshot: the commercial roster
    # alternates by ISO-week parity, so on (say) an even-week publish the
    # immediately-prior snapshot holds the odd-week model only. Breaking there
    # would leave every even-week model without a prior and silently drop it
    # from the headline cards. Accumulating per-key instead pairs each model
    # with the previous week *it* ran (matching the manifest's drift logic).
    prior_metrics_by_key: dict[tuple[str, str], "MetricRecord"] = {}
    for h in reversed(manifest.history):
        for m in h.metrics:
            key = (m.prompt_id, m.model_id)
            if key not in prior_metrics_by_key:
                prior_metrics_by_key[key] = m
    if not prior_metrics_by_key:
        return []

    prompts_by_id = {p.prompt_id: p for p in manifest.prompts}
    models_by_id = {m.model_id: m for m in manifest.models}
    shifts: list[dict] = []
    for cur in manifest.metrics:
        prior = prior_metrics_by_key.get((cur.prompt_id, cur.model_id))
        if prior is None:
            continue
        prompt = prompts_by_id.get(cur.prompt_id)
        model = models_by_id.get(cur.model_id)
        if prompt is None or model is None:
            continue

        # Refusal rate.
        d = abs(cur.refusal_rate - prior.refusal_rate)
        shifts.append({
            "metric": "refusal_rate", "metric_label": "Refusal rate",
            "prompt_id": cur.prompt_id, "prompt_title": prompt.title,
            "model_id": cur.model_id, "model_name": model.display_name,
            "axis": prompt.axis,
            "from_value": round(prior.refusal_rate, 3),
            "to_value": round(cur.refusal_rate, 3),
            "delta": round(cur.refusal_rate - prior.refusal_rate, 3),
            "magnitude": d / _DRIFT_SCALES["refusal_rate"],
        })
        # Hedge density. Skipped when either end has no text, for the
        # same reason the length branch below is: a cell measured
        # entirely from body-less refusals publishes a null hedge
        # density, and treating that as 0 would rank a non-measurement
        # as one of the largest framing shifts of the week.
        if cur.hedge_density is not None and prior.hedge_density is not None:
            d = abs(cur.hedge_density - prior.hedge_density)
            shifts.append({
                "metric": "hedge_density", "metric_label": "Hedge density",
                "prompt_id": cur.prompt_id, "prompt_title": prompt.title,
                "model_id": cur.model_id, "model_name": model.display_name,
                "axis": prompt.axis,
                "from_value": round(prior.hedge_density, 2),
                "to_value": round(cur.hedge_density, 2),
                "delta": round(cur.hedge_density - prior.hedge_density, 2),
                "magnitude": d / _DRIFT_SCALES["hedge_density"],
            })
        # Length median (relative shift). Skipped when either end has no
        # length at all: a cell measured entirely from body-less refusals
        # publishes a null median, and treating that as 0 would rank a
        # non-measurement as the largest length shift of the week.
        if prior.length.median and cur.length.median is not None:
            denom = max(prior.length.median, cur.length.median) / 2 or 1.0
            d = abs(cur.length.median - prior.length.median) / denom
            shifts.append({
                "metric": "length_median", "metric_label": "Length (median)",
                "prompt_id": cur.prompt_id, "prompt_title": prompt.title,
                "model_id": cur.model_id, "model_name": model.display_name,
                "axis": prompt.axis,
                "from_value": int(prior.length.median),
                "to_value": int(cur.length.median),
                "delta": int(cur.length.median - prior.length.median),
                "magnitude": d,
            })

    shifts.sort(key=lambda s: s["magnitude"], reverse=True)
    return shifts[:top_n]


def drift_baselines(manifest: Manifest) -> list[dict]:
    """Which weeks this snapshot's drift p-values were computed against.

    A drift p-value with no stated baseline invites exactly one reading,
    "this changed since last week", and that reading is often wrong. The
    commercial roster alternates by ISO-week parity, so a frontier model
    is normally compared with the previous week *it* ran, two calendar
    weeks back. After the 2026-W30 and 2026-W31 outage the nearest prior
    run for an even-cadence model was four weeks back, and nothing on
    the site said so.

    Returns one row per distinct (comparison week, weeks elapsed),
    newest first, with the number of drift tests resting on it. Empty
    for manifests published before ``DriftTest.compared_to_week``
    existed, in which case the template falls back to describing the
    rule rather than naming the weeks.
    """
    counts: dict[tuple[str, int | None], int] = {}
    for m in manifest.metrics:
        for d in (m.refusal_drift, m.hedge_drift, m.length_drift):
            if d is None or not d.compared_to_week:
                continue
            key = (d.compared_to_week, d.weeks_elapsed)
            counts[key] = counts.get(key, 0) + 1
    rows = [
        {"week_id": week, "weeks_elapsed": elapsed, "tests": n}
        for (week, elapsed), n in counts.items()
    ]
    rows.sort(key=lambda r: r["week_id"], reverse=True)
    return rows


#: How many baseline weeks the drift-tests note names before summarising
#: the remainder as "and N more". Three is a readability cap on one
#: table cell, not a limit on what is disclosed: the same page lists
#: every baseline in full in the Statistical rigor section (the
#: #drift-baseline paragraph), so a reader who sees "and 2 more" here
#: has the complete list a screen further down, and the note says where.
#: Raise the cap only if that paragraph goes away.
_DRIFT_NOTE_BASELINE_CAP = 3


def _drift_note(manifest: Manifest, baselines: list[dict]) -> str:
    """Note cell for the drift-tests row of "What's measured this week"."""
    if not any(m.refusal_drift is not None for m in manifest.metrics):
        return "Need a prior week's samples per pair before tests fire"
    base = "BH-corrected at FDR 0.05 across the within-week family"
    if not baselines:
        return base
    parts = []
    for row in baselines[:_DRIFT_NOTE_BASELINE_CAP]:
        elapsed = row["weeks_elapsed"]
        if elapsed is None:
            parts.append(row["week_id"])
        elif elapsed == 1:
            parts.append(f"{row['week_id']} (1 week back)")
        else:
            parts.append(f"{row['week_id']} ({elapsed} weeks back)")
    hidden = len(baselines) - _DRIFT_NOTE_BASELINE_CAP
    more = "" if hidden <= 0 else f", and {hidden} more (full list under Statistical rigor)"
    return f"{base}; compared against {', '.join(parts)}{more}"


def _metric_status(manifest: Manifest) -> list[dict]:
    """Inspect the latest manifest and report which metrics are
    actually populated this week. Powers the "What's measured today"
    table on /methodology/ — sourced from the live manifest at build
    time so the table can never lie about what shipped.
    """
    if not manifest.metrics:
        return []
    sample = manifest.metrics[0]
    has_drift = any(m.refusal_drift is not None for m in manifest.metrics)
    has_change_points = any(
        bool(m.change_points.refusal_rate
             or m.change_points.hedge_density
             or m.change_points.length_median)
        for m in manifest.metrics
    )
    has_stance_signal = any(m.stance != "na" for m in manifest.metrics)
    has_embedding = any(
        m.embedding_centroid_shift is not None for m in manifest.metrics
    )
    has_silent_update = bool(manifest.silent_update_warnings)

    return [
        {"metric": "Refusal rate", "status": "live",
         "note": f"e.g. {sample.refusal_rate:.2f} on first metric record"},
        {"metric": "Hedge density", "status": "live",
         "note": (
             f"e.g. {sample.hedge_density:.2f} markers/100 tok on first record"
             if sample.hedge_density is not None
             # Null means the first record's cell carried no text, same
             # case the length note below describes.
             else "null on the first record: that cell carried no response text"
         )},
        {"metric": "Length distribution", "status": "live",
         "note": (
             f"median, p25/p75 — first record median = {sample.length.median:.0f}"
             if sample.length.median is not None
             # Null median means the first record's cell carried no text
             # to measure, which is a real state since 2026-W32 rather
             # than a missing metric.
             else "median, p25/p75 — first record carried no text to measure"
         )},
        {"metric": "Drift tests (refusal/hedge/length)",
         "status": "live" if has_drift else "data-gated",
         "note": _drift_note(manifest, drift_baselines(manifest))},
        {"metric": "Change-point detection (PELT)",
         "status": "live" if has_change_points else "data-gated",
         "note": ("Annotates per-(prompt,model,metric) sparkline series"
                  if has_change_points
                  else "Needs ≥ 4 weeks of paired history per pair")},
        {"metric": "Stance",
         "status": "live" if has_stance_signal else "off",
         "note": ("Haiku-classified on stance-bearing axes"
                  if has_stance_signal
                  else "Currently off; metric record stance='na' on every row")},
        {"metric": "Embedding centroid shift",
         "status": "live" if has_embedding else "off",
         "note": ("Sentence-transformers cosine-distance week over week"
                  if has_embedding
                  else "No embedding_centroid_shift on any row this week")},
        {"metric": "Silent-update warnings",
         "status": "live" if has_silent_update else "live (no flags this week)",
         "note": ("Anomalies on the neutral-control axis"
                  if has_silent_update
                  else "No neutral-control anomalies surfaced this snapshot")},
    ]


def og_available() -> bool:
    """True when `site/src/og.py` can render PNGs.

    Called before render_page() so base.html knows whether to emit
    `<meta og:image>` URLs pointing at PNGs or fall back to the default
    SVG. The matplotlib import is the real gate; doing it here avoids
    committing to a PNG URL in HTML that never gets written to disk."""
    try:
        import matplotlib  # noqa: F401
        from og import render_og_png  # noqa: F401
    except ImportError:
        return False
    return True


def publish_og_images(
    out_dir: Path,
    manifest: Manifest,
    reports: list[Report],
) -> int:
    """Best-effort OG PNG generation for the minimum-coverage page set.

    Returns the number of PNGs written. 0 means matplotlib is missing
    (warning printed to stderr); the site still builds and falls back
    to the default SVG placeholder per base.html.
    """
    try:
        from og import render_og_png
    except ImportError:
        print(
            "[og] skipping: site/src/og.py import failed",
            file=sys.stderr,
        )
        return 0

    axes = sorted({p.axis for p in manifest.prompts})
    specs: list[tuple[str, str, str]] = [
        ("index", "Meridian",
         "A public record of how commercial LLMs change over time"),
        ("methodology", "Methodology",
         "How Meridian measures drift on contested topics"),
    ]
    for r in reports:
        specs.append((
            f"report-{r.slug}",
            r.title,
            r.summary or "Meridian report",
        ))
    for m in manifest.models:
        specs.append((
            f"model-{m.model_id}",
            m.display_name,
            f"{m.provider} · {m.version_string}",
        ))
    for p in manifest.prompts:
        specs.append((
            f"prompt-{p.prompt_id}",
            p.title,
            f"Prompt on axis: {p.axis.replace('-', ' ')}",
        ))
    for axis in axes:
        specs.append((
            f"axis-{axis}",
            axis.replace("-", " ").capitalize(),
            "Meridian prompt axis",
        ))

    og_dir = out_dir / "static" / "og"
    og_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for slug, title, subtitle in specs:
        try:
            render_og_png(title, subtitle, og_dir / f"{slug}.png")
            written += 1
        except RuntimeError as e:
            print(f"[og] skipping {slug!r}: {e}", file=sys.stderr)
            return written
    return written


def _safe_clean_dist(out_dir: Path) -> None:
    """Wipe out_dir so stale pages from prior builds can't pollute
    sitemap.xml / urls.txt / the link-rot guard.

    Guarded: we only wipe when the directory is empty or carries a
    `build.json` marker left by a previous build. This refuses to
    delete directories we don't recognise as our own output — the
    existence check catches "oops, user pointed --out at a real
    directory" before it's too late.
    """
    if not out_dir.exists():
        return
    contents = list(out_dir.iterdir())
    if not contents:
        return
    marker = out_dir / "build.json"
    if not marker.exists():
        raise SystemExit(
            f"refusing to wipe {out_dir}: not empty and no build.json "
            f"marker (which every meridian build leaves). If this is a "
            f"previous meridian build that somehow lost its marker, "
            f"remove the directory manually and re-run."
        )
    shutil.rmtree(out_dir)


def build(manifest_path: Path, out_dir: Path) -> dict:
    manifest = load_manifest(manifest_path)
    _safe_clean_dist(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    env = jinja_env(TEMPLATES_DIR)
    build_meta = {
        "git_sha": git_sha(REPO_ROOT),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "manifest_week": manifest.snapshot.week_id,
        "manifest_path": manifest_path.name,
        "schema_version": SCHEMA_VERSION,
    }
    reports = load_reports(CONTENT_REPORTS)

    # Probe OG availability up-front so base.html only emits PNG URLs
    # when PNGs will actually land on disk. Actual emission happens
    # after copy_static (which wipes dist/static/).
    og_ok = og_available()

    # og_slug is inherited by every template. Pages that need a custom
    # OG image override it; base.html checks og_available to decide
    # between the PNG and the SVG fallback.
    base_context = {
        "manifest": manifest, "build": build_meta,
        "site_title": "Meridian",
        "og_slug": None, "og_available": og_ok,
        "weekly_summaries": load_run_log_summary(REPO_ROOT),
        "metric_status": _metric_status(manifest),
        "drift_baselines": drift_baselines(manifest),
        "heatmap": drift_heatmap(manifest),
        "notable": notable_shifts(manifest),
        "model_refusal_series": model_refusal_series(manifest),
    }

    # Pages in STATIC_PAGES that get their own OG PNG. Tuple of
    # (template_name, og_slug).
    _STATIC_OG_SLUGS = {
        "index.html": "index",
        "methodology.html": "methodology",
    }

    for template_name, out_path in STATIC_PAGES:
        if (TEMPLATES_DIR / template_name).exists():
            ctx = base_context
            slug = _STATIC_OG_SLUGS.get(template_name)
            if slug is not None:
                ctx = dict(base_context, og_slug=slug)
            render_page(env, template_name, out_dir / out_path, ctx)

    # Always render the reports index and Atom feed, even with zero
    # reports authored. Both URLs ship in the public urls.txt and
    # citation-stability says we don't 404 them. The empty-state copy
    # is in reports_index.html; the empty Atom feed is still valid.
    render_reports(env, out_dir, reports, base_context)
    write_atom_feed(env, out_dir, reports, base_context)

    render_dashboard(env, out_dir, manifest, base_context)
    publish_data(env, out_dir, manifest_path, manifest, base_context)

    # MERIDIAN_REDIRECTS overrides the map's location. Production never
    # sets it, so the canonical path stays the only one that ships. It
    # exists so a test can script the redirect map without writing to
    # the real one: the tests used to swap site/redirects.yaml in place
    # and restore it in a finally block, and on 2026-08-15 that same
    # pattern applied to data/run_log.jsonl destroyed 15 of 17 entries
    # when two pytest processes overlapped. A lost restore here is worse
    # than untidy, a dropped redirect row is a published URL that starts
    # serving a 404, which the never-404 rule exists to prevent.
    redirects_override = os.environ.get("MERIDIAN_REDIRECTS")
    redirects_path = (
        Path(redirects_override)
        if redirects_override
        else REPO_ROOT / "site" / "redirects.yaml"
    )
    redirects_written = write_redirects(env, redirects_path, out_dir, base_context)
    if redirects_written:
        print(f"emitted {redirects_written} redirect page(s)")

    write_robots(out_dir)
    write_humans(out_dir, build_meta)
    write_sitemap(out_dir)
    write_cname(out_dir)

    copy_static(STATIC_DIR, out_dir)
    # Must run after copy_static, which wipes and repopulates dist/static/.
    write_search_index(out_dir, manifest, reports)
    if og_ok:
        og_written = publish_og_images(out_dir, manifest, reports)
        if og_written:
            print(f"emitted {og_written} OG PNG(s)")
    (out_dir / "build.json").write_text(
        json.dumps(build_meta, indent=2, sort_keys=True) + "\n"
    )

    urls = collect_urls(out_dir)
    (out_dir / "urls.txt").write_text("\n".join(sorted(urls)) + "\n")

    print(
        f"built {manifest.snapshot.week_id} -> {out_dir} "
        f"({build_meta['git_sha'][:7]}, {len(urls)} url(s))"
    )
    return build_meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Meridian static site.")
    parser.add_argument(
        "--manifest", type=Path, required=True, help="Path to manifest JSON"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("site/dist"),
        help="Output directory (default: site/dist)",
    )
    args = parser.parse_args(argv)
    build(args.manifest.resolve(), args.out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
