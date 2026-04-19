#!/usr/bin/env python3
"""Drift Audit static site builder.

Consumes a validated manifest JSON and renders the site to dist/.
Idempotent and deterministic: given the same inputs, produces byte-identical output
(modulo the build timestamp, which is surfaced in build.json for provenance).

Run:
    uv run python site/src/build.py --manifest site/fixtures/manifest-2026-W16.json \
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
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup
from pydantic import ValidationError

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from chart import OKABE_ITO, heatmap_cell_style, sparkline, viridis_color  # noqa: E402
from schema import SCHEMA_VERSION, Manifest  # noqa: E402

TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"
REPO_ROOT = _HERE.parent.parent
CONTENT_REPORTS = REPO_ROOT / "site" / "content" / "reports"

SITE_ORIGIN = "https://drift-audit.example"

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
    ("404.html", "404.html"),
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
        return Manifest.model_validate(raw)
    except ValidationError as e:
        raise SystemExit(f"manifest validation failed:\n{e}") from e


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
        ignore=shutil.ignore_patterns(".gitkeep"),
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
        ctx = dict(base_context, report=r)
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
        readme_text = (
            f"# Drift Audit snapshot: {week_id}\n\n"
            f"Contains metrics for {len(metrics)} (prompt x model) observations "
            f"captured during ISO week {week_id}.\n\n"
            f"Columns: {', '.join(_METRICS_COLUMNS)}\n\n"
            f"License: CC-BY-SA 4.0. See /data/schema/ for full column definitions.\n"
            f"Citation: https://drift-audit.example/data/{week_id}/\n"
        )

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
        (snap_dir / "SHA256SUMS").write_text("\n".join(sorted(sums)) + "\n")

        snapshots_meta.append(
            {
                "week_id": week_id,
                "is_current": week_id == manifest.snapshot.week_id,
                "files": [
                    {"name": name, "size": len(text.encode("utf-8"))}
                    for name, text in artifacts.items()
                ],
                "row_count": len(metrics),
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


def write_robots(out_dir: Path) -> None:
    (out_dir / "robots.txt").write_text(
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {SITE_ORIGIN}/sitemap.xml\n"
    )


def write_humans(out_dir: Path, build_meta: dict) -> None:
    lines = [
        "/* TEAM */",
        "Project: Drift Audit",
        "Site: https://drift-audit.example/",
        "Source: https://github.com/drift-audit/meridian",
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


def build(manifest_path: Path, out_dir: Path) -> dict:
    manifest = load_manifest(manifest_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    env = jinja_env(TEMPLATES_DIR)
    build_meta = {
        "git_sha": git_sha(REPO_ROOT),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "manifest_week": manifest.snapshot.week_id,
        "manifest_path": manifest_path.name,
        "schema_version": SCHEMA_VERSION,
    }
    base_context = {"manifest": manifest, "build": build_meta, "site_title": "Drift Audit"}

    for template_name, out_path in STATIC_PAGES:
        if (TEMPLATES_DIR / template_name).exists():
            render_page(env, template_name, out_dir / out_path, base_context)

    reports = load_reports(CONTENT_REPORTS)
    if reports:
        render_reports(env, out_dir, reports, base_context)
        write_atom_feed(env, out_dir, reports, base_context)

    render_dashboard(env, out_dir, manifest, base_context)
    publish_data(env, out_dir, manifest_path, manifest, base_context)

    write_robots(out_dir)
    write_humans(out_dir, build_meta)
    write_sitemap(out_dir)

    copy_static(STATIC_DIR, out_dir)
    # Must run after copy_static, which wipes and repopulates dist/static/.
    write_search_index(out_dir, manifest, reports)
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
    parser = argparse.ArgumentParser(description="Build the Drift Audit static site.")
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
