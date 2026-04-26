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

from chart import OKABE_ITO, heatmap_cell_style, sparkline, viridis_color  # noqa: E402
from schema import SCHEMA_VERSION, Manifest  # noqa: E402

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


def jinja_env(templates_dir: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(templates_dir),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["sparkline"] = sparkline
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
    axes = sorted({p.axis for p in manifest.prompts})

    # ---- per-model pages + index ----
    for m in manifest.models:
        ctx = dict(
            base_context,
            model=m,
            weeks=weeks,
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
        # Model x week drill-down: only the current week gets a page for v1.
        render_page(
            env, "model_week.html",
            out_dir / "models" / m.model_id / manifest.snapshot.week_id / "index.html",
            dict(ctx, week_id=manifest.snapshot.week_id,
                 week_metrics=[mx for mx in manifest.metrics if mx.model_id == m.model_id]),
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
            axis_prompts=axis_prompts,
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
    for p in manifest.prompts:
        ctx = dict(
            base_context,
            prompt=p,
            models=manifest.models,
            weeks=weeks,
            manifest=manifest,
            timeseries=manifest.timeseries,
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
    "week_id", "prompt_id", "model_id", "n_samples",
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
            "refusal_rate": float(m.refusal_rate),
            "refusal_ci_lower": float(m.refusal_ci.lower),
            "refusal_ci_upper": float(m.refusal_ci.upper),
            "hedge_density": float(m.hedge_density),
            "length_median": float(m.length.median),
            "length_p25": float(m.length.p25),
            "length_p75": float(m.length.p75),
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


def _metrics_to_csv(week_id: str, metrics: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(_METRICS_COLUMNS)
    for m in metrics:
        w.writerow([
            week_id, m.prompt_id, m.model_id, m.n_samples,
            m.refusal_rate, m.refusal_ci.lower, m.refusal_ci.upper,
            m.hedge_density, m.length.median, m.length.p25, m.length.p75,
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
        snapshots_meta.append(
            {
                "week_id": week_id,
                "is_current": week_id == manifest.snapshot.week_id,
                "files": files_meta,
                "row_count": len(metrics),
                "has_responses": responses_meta is not None,
            }
        )

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
    """
    from meridian.pipeline.run_log import read_run_log
    from meridian.pipeline.run_log_summary import summarize_weekly

    log_path = repo_root / "data" / "run_log.jsonl"
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
            "cells": {}, "max_score": 1.0, "mode": "absolute",
            "has_stale": False,
        }
    max_score = max(c["score"] for c in cells.values()) or 1.0
    mode = "delta" if any(c["mode"] == "delta" for c in cells.values()) else "absolute"
    has_stale = any(
        c["as_of_week"] != manifest.snapshot.week_id for c in cells.values()
    )
    return {
        "axes": axes, "models": models,
        "cells": cells, "max_score": max_score, "mode": mode,
        "has_stale": has_stale,
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
    prior_metrics_by_key: dict[tuple[str, str], "MetricRecord"] = {}
    for h in reversed(manifest.history):
        for m in h.metrics:
            key = (m.prompt_id, m.model_id)
            if key not in prior_metrics_by_key:
                prior_metrics_by_key[key] = m
        if prior_metrics_by_key:
            break
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
        # Hedge density.
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
        # Length median (relative shift).
        if prior.length.median:
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
         "note": f"e.g. {sample.hedge_density:.2f} markers/100 tok on first record"},
        {"metric": "Length distribution", "status": "live",
         "note": f"median, p25/p75 — first record median = {sample.length.median:.0f}"},
        {"metric": "Drift tests (refusal/hedge/length)",
         "status": "live" if has_drift else "data-gated",
         "note": ("BH-corrected at FDR 0.05 across the within-week family"
                  if has_drift
                  else "Need a prior week's samples per pair before tests fire")},
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
                  else "Currently off (config: embedding.enabled=false)")},
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

    redirects_path = REPO_ROOT / "site" / "redirects.yaml"
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
