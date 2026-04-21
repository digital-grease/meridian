#!/usr/bin/env python3
"""Regenerate PNG favicon fallbacks from the canonical SVG.

The SVG at ``site/src/static/images/favicon.svg`` is the single source
of truth. Re-run this script whenever that file changes; the PNGs are
committed so the site build doesn't need rsvg-convert at build time.

Requires the ``rsvg-convert`` binary (from librsvg). Verified available
on the project's CI runners.

Run:
    uv run python scripts/generate_favicons.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "site" / "src" / "static" / "images" / "favicon.svg"

# (png filename, pixel size). 16/32 for classic desktop, 180 for Apple,
# 192/512 for PWA install icons per site.webmanifest.
SIZES: tuple[tuple[str, int], ...] = (
    ("favicon-16.png", 16),
    ("favicon-32.png", 32),
    ("favicon-180.png", 180),
    ("favicon-192.png", 192),
    ("favicon-512.png", 512),
)


def main() -> int:
    if shutil.which("rsvg-convert") is None:
        print("rsvg-convert not found; install librsvg (package `librsvg2-bin` on "
              "Debian/Ubuntu, `librsvg` on macOS).", file=sys.stderr)
        return 2
    if not SRC.exists():
        print(f"source SVG missing: {SRC}", file=sys.stderr)
        return 2

    out_dir = SRC.parent
    for name, sz in SIZES:
        out_path = out_dir / name
        subprocess.run(
            ["rsvg-convert", "-w", str(sz), "-h", str(sz),
             str(SRC), "-o", str(out_path)],
            check=True,
        )
        print(f"wrote {out_path.relative_to(REPO)} ({sz}x{sz})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
