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
    return VIRIDIS[_viridis_index(t)]


def _viridis_index(t: float) -> int:
    """Sqrt-shaped index lookup. Linear in t left almost the entire
    grid in viridis-purple when one outlier cell pinned the top of
    the scale (the typical case: a refusal-boundary cell at refusal=0.8
    dominates everything else at refusal<0.05). Sqrt expands the low
    end so small-but-non-zero scores get visible differentiation."""
    t = max(0.0, min(1.0, t)) ** 0.5
    return min(len(VIRIDIS) - 1, int(t * (len(VIRIDIS) - 1) + 0.5))


def _is_measured(value: object) -> bool:
    """True for a real plottable number, False for a gap marker.

    Kept local (rather than imported from ``schema``) so this module
    stays dependency-free and works for any caller that signals a gap
    with ``None``. Booleans are rejected because ``bool`` subclasses
    ``int`` and a stray ``True`` would silently plot as 1.0.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def sparkline(
    values: list[float | None],
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

    Any entry that is not a real number (``None``, or
    ``schema.MISSING_WEEK``) is a gap: a week the audit did not run. The
    line is drawn as one polyline per contiguous run of measurements and
    is *not* carried across the gap. Before 2026-W30 this function
    always emitted a single unbroken polyline, so the two-week
    InsufficientInstanceCapacity outage would have rendered as W29
    joining straight onto W32, drawing a hole in the record as
    continuity, while the methodology page told readers such weeks
    appear "as a break in the line". Both the gap slot on the axis and
    the break are the point: the horizontal position of every remaining
    measurement stays true to the calendar.

    A run of exactly one measurement between two gaps cannot be a line,
    so it is drawn as a dot. Dropping it would hide a real measurement.
    """
    if not values:
        return Markup('<span class="muted">no data</span>')
    measured = [v for v in values if _is_measured(v)]
    if not measured:
        return Markup('<span class="muted">no data</span>')
    lo = min(measured)
    hi = max(measured)
    span = hi - lo or 1.0
    n = len(values)

    def _y(v: float) -> float:
        return height - ((v - lo) / span) * height

    # Contiguous runs of measured points, each becoming its own polyline.
    segments: list[list[tuple[float, float]]] = []
    if n == 1:
        step_size = 0.0
        # A single point has no horizontal extent to draw across, so the
        # long-standing behaviour is a flat full-width line.
        segments.append([(0.0, height / 2), (float(width), height / 2)])
    else:
        step_size = width / (n - 1)
        run: list[tuple[float, float]] = []
        for i, v in enumerate(values):
            if _is_measured(v):
                run.append((i * step_size, _y(v)))
            elif run:
                segments.append(run)
                run = []
        if run:
            segments.append(run)

    shapes: list[str] = []
    for run in segments:
        if len(run) == 1:
            x, y = run[0]
            shapes.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.75" fill="{color}" />'
            )
        else:
            pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in run)
            shapes.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
                f'stroke-linecap="round" stroke-linejoin="round" '
                f'points="{pts}" />'
            )
    shapes_svg = "".join(shapes)

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
    if len(measured) != n:
        base_label = f"{base_label} (line breaks where no run happened)"
    if marker_parts:
        base_label = f"{base_label} (change-point marked)"
    title = escape(base_label)
    values_text = escape(
        ", ".join(
            value_format.format(v) if _is_measured(v) else "no run"
            for v in values
        )
    )
    return Markup(
        f'<svg class="sparkline" role="img" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" aria-label="{title}: {values_text}">'
        f"<title>{title}: {values_text}</title>"
        f"{markers_svg}"
        f"{shapes_svg}</svg>"
    )


def heatmap_cell_style(value: float, lo: float = 0.0, hi: float = 1.0) -> str:
    """CSS ``style`` attribute value for a heatmap cell coloured via viridis.

    Background and foreground are co-selected from
    :data:`VIRIDIS_FG` so the contrast pair is guaranteed across the
    whole index range — no cliff at the transition between two indices.
    Index lookup uses :func:`_viridis_index`'s sqrt shape so the low
    end of the score range gets visible differentiation when a single
    outlier pins the top of the scale.
    """
    if hi <= lo:
        idx = 0
    else:
        t = (value - lo) / (hi - lo)
        idx = _viridis_index(t)
    return f"background:{VIRIDIS[idx]};color:{VIRIDIS_FG[idx]}"
