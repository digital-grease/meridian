"""Inline SVG and CSS helpers for the Drift Audit dashboard.

No heavy dependencies: all charts are pre-rendered to SVG at build time so
the site serves entirely static content and works with JavaScript disabled.
"""
from __future__ import annotations

from markupsafe import Markup, escape

# Viridis 5-step (colorblind-safe sequential palette).
VIRIDIS = ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"]

# Okabe-Ito categorical palette.
OKABE_ITO = [
    "#000000", "#e69f00", "#56b4e9", "#009e73",
    "#f0e442", "#0072b2", "#d55e00", "#cc79a7",
]


def viridis_color(value: float, lo: float = 0.0, hi: float = 1.0) -> str:
    if hi <= lo:
        return VIRIDIS[0]
    t = max(0.0, min(1.0, (value - lo) / (hi - lo)))
    idx = min(len(VIRIDIS) - 1, int(t * (len(VIRIDIS) - 1) + 0.5))
    return VIRIDIS[idx]


def sparkline(
    values: list[float],
    *,
    width: int = 140,
    height: int = 28,
    color: str = "currentColor",
    label: str = "",
    value_format: str = "{:.2f}",
) -> Markup:
    """Inline SVG sparkline.

    Accessibility: sets ``role="img"``, ``aria-label``, and a ``<title>``
    with every value so screen readers announce the series. Templates
    should follow up with a data table for full programmatic access.
    """
    if not values:
        return Markup('<span class="muted">no data</span>')
    lo = min(values)
    hi = max(values)
    span = hi - lo or 1.0
    n = len(values)
    if n == 1:
        points = f"0,{height / 2:.1f} {width},{height / 2:.1f}"
    else:
        step = width / (n - 1)
        points = " ".join(
            f"{i * step:.1f},{height - ((v - lo) / span) * height:.1f}"
            for i, v in enumerate(values)
        )
    title = escape(label or "sparkline")
    values_text = escape(", ".join(value_format.format(v) for v in values))
    return Markup(
        f'<svg class="sparkline" role="img" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" aria-label="{title}: {values_text}">'
        f"<title>{title}: {values_text}</title>"
        f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round" '
        f'points="{points}" /></svg>'
    )


def heatmap_cell_style(value: float, lo: float = 0.0, hi: float = 1.0) -> str:
    """CSS ``style`` attribute value for a heatmap cell coloured via viridis."""
    color = viridis_color(value, lo, hi)
    # White-on-dark or black-on-light for legibility across the palette.
    # Viridis goes purple->yellow; index >= 3 gets dark text.
    t = max(0.0, min(1.0, (value - lo) / (hi - lo))) if hi > lo else 0.0
    fg = "#111" if t >= 0.66 else "#fff"
    return f"background:{color};color:{fg}"
