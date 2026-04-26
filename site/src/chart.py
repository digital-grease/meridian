"""Inline SVG and CSS helpers for the Meridian dashboard.

No heavy dependencies: all charts are pre-rendered to SVG at build time so
the site serves entirely static content and works with JavaScript disabled.
"""
from __future__ import annotations

from markupsafe import Markup, escape

# Viridis 5-step (colorblind-safe sequential palette).
VIRIDIS = ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"]

# Per-viridis-index foreground colour. Fixed map (rather than a t
# threshold) so the bg/fg pair is always co-decided and contrast can
# never land in a cliff between indices. WCAG AA contrast values
# against each bg with the chosen fg, smallest first:
#   idx 0 #440154 + #fff = 15.4 :1
#   idx 1 #3b528b + #fff =  7.4 :1
#   idx 2 #21918c + #111 =  6.4 :1   (white was 3.82, FAIL)
#   idx 3 #5ec962 + #111 = 11.4 :1
#   idx 4 #fde725 + #111 = 17.7 :1
VIRIDIS_FG = ["#fff", "#fff", "#111", "#111", "#111"]

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
    change_point_indices: list[int] | None = None,
) -> Markup:
    """Inline SVG sparkline.

    ``change_point_indices`` draws vertical dashed markers at the given
    indices. Indices are validated as integers in range; any other value
    is silently dropped to keep the SVG injection-safe.
    """
    if not values:
        return Markup('<span class="muted">no data</span>')
    lo = min(values)
    hi = max(values)
    span = hi - lo or 1.0
    n = len(values)
    if n == 1:
        points = f"0,{height / 2:.1f} {width},{height / 2:.1f}"
        step_size = 0.0
    else:
        step_size = width / (n - 1)
        points = " ".join(
            f"{i * step_size:.1f},{height - ((v - lo) / span) * height:.1f}"
            for i, v in enumerate(values)
        )

    # Build change-point markers from VALIDATED integer indices only.
    # Everything composed into the SVG here is numeric after validation.
    marker_parts: list[str] = []
    if change_point_indices:
        for raw_idx in change_point_indices:
            if not isinstance(raw_idx, int):
                continue
            if not (0 < raw_idx < n):
                continue
            x = float(raw_idx) * step_size
            marker_parts.append(
                f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{height}" '
                f'stroke="#d55e00" stroke-width="1" stroke-dasharray="2,2" />'
            )
    markers_svg = "".join(marker_parts)

    # Compose the human-readable label, then escape exactly once.
    base_label = label or "sparkline"
    if marker_parts:
        base_label = f"{base_label} (change-point marked)"
    title = escape(base_label)
    values_text = escape(", ".join(value_format.format(v) for v in values))
    return Markup(
        f'<svg class="sparkline" role="img" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" aria-label="{title}: {values_text}">'
        f"<title>{title}: {values_text}</title>"
        f"{markers_svg}"
        f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round" '
        f'points="{points}" /></svg>'
    )


def heatmap_cell_style(value: float, lo: float = 0.0, hi: float = 1.0) -> str:
    """CSS ``style`` attribute value for a heatmap cell coloured via viridis.

    Background and foreground are co-selected from
    :data:`VIRIDIS_FG` so the contrast pair is guaranteed across the
    whole index range — no cliff at the transition between two indices.
    """
    if hi <= lo:
        idx = 0
    else:
        t = max(0.0, min(1.0, (value - lo) / (hi - lo)))
        idx = min(len(VIRIDIS) - 1, int(t * (len(VIRIDIS) - 1) + 0.5))
    return f"background:{VIRIDIS[idx]};color:{VIRIDIS_FG[idx]}"
