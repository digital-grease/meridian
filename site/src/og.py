"""Open Graph PNG generator.

Raster 1200×630 social-preview images for each public page. Social
scrapers (Twitter, Slack, Discord) handle PNG reliably; SVG is
inconsistently supported.

`matplotlib` is the only runtime dependency (already in the `charts`
optional dep group). We do not add pillow or cairo; they'd be redundant
and expand the install footprint for a build-time concern.

If `matplotlib` is not installed, `render_og_png` raises a RuntimeError
directing the caller to `uv sync --group charts`. Callers (`build.py`)
catch this and skip OG generation with a stderr warning — the site still
builds; pages just fall back to the SVG placeholder.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

# Matches the favicon's primary blue — keeps the social preview visually
# consistent with the site icon across clients that render both.
BG_COLOR = "#0b2545"
TITLE_COLOR = "#ffffff"
SUBTITLE_COLOR = "#c4d4e8"
WORDMARK_COLOR = "#8aa6c7"

_STATIC_IMAGES = Path(__file__).resolve().parent / "static" / "images"
_FAVICON_PATH = _STATIC_IMAGES / "favicon-192.png"

# Pixel dims at dpi=100. Matches the 1200×630 Open Graph convention.
_FIGSIZE = (12.0, 6.3)
_DPI = 100


def render_og_png(title: str, subtitle: str, out_path: Path) -> None:
    """Render one OG PNG. Raises RuntimeError if matplotlib is missing."""
    try:
        import matplotlib

        matplotlib.use("Agg")  # headless
        import matplotlib.image as mpimg
        import matplotlib.pyplot as plt
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "render_og_png requires the `charts` dep group. "
            "Install with: uv sync --group charts"
        ) from e

    fig = plt.figure(figsize=_FIGSIZE, dpi=_DPI, facecolor=BG_COLOR)

    # Title: wrap to roughly two lines at the chosen font size.
    title_wrapped = "\n".join(textwrap.wrap(title, width=28)) or title
    fig.text(
        0.05, 0.82, title_wrapped,
        fontsize=44, color=TITLE_COLOR,
        weight="bold", family="sans-serif",
        ha="left", va="top",
    )

    subtitle_wrapped = "\n".join(textwrap.wrap(subtitle, width=52)) or subtitle
    fig.text(
        0.05, 0.38, subtitle_wrapped,
        fontsize=22, color=SUBTITLE_COLOR,
        family="sans-serif",
        ha="left", va="top",
    )

    fig.text(
        0.05, 0.08, "meridianaudit.org",
        fontsize=18, color=WORDMARK_COLOR,
        family="monospace",
        ha="left", va="bottom",
    )

    if _FAVICON_PATH.exists():
        img = mpimg.imread(str(_FAVICON_PATH))
        ax = fig.add_axes((0.87, 0.08, 0.08, 0.15))
        ax.imshow(img)
        ax.axis("off")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        out_path, format="png", dpi=_DPI, facecolor=fig.get_facecolor(),
    )
    plt.close(fig)
