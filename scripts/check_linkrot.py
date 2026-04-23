#!/usr/bin/env python3
"""Link-rot guard for the Meridian site.

Compare URL sets between the new build and a previous build snapshot.
Fail with exit 1 if any previously-published URL is missing from the
new build. Citation stability is a hard project guarantee; this is the
mechanism that enforces it in CI.

URLs are read from ``urls.txt`` files produced by ``build.py``. If a URL
needs to be retired deliberately (extremely rare), add it to the
allowlist file to acknowledge the breakage in review.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def load_urls(urls_file: Path) -> set[str]:
    if not urls_file.exists():
        return set()
    return {
        line.strip()
        for line in urls_file.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Fail if any previously-published URL is missing from the new build."
    )
    p.add_argument(
        "--current",
        type=Path,
        required=True,
        help="Current build directory (must contain urls.txt)",
    )
    p.add_argument(
        "--previous",
        type=Path,
        required=True,
        help="Previous build directory (urls.txt optional; absent = first build)",
    )
    p.add_argument(
        "--allow-missing",
        type=Path,
        default=Path(".allow-missing-urls"),
        help="Optional allowlist of URLs permitted to disappear (one per line).",
    )
    args = p.parse_args(argv)

    current_file = args.current / "urls.txt"
    if not current_file.exists():
        print(
            f"ERROR: current build {args.current} has no urls.txt. Did build.py run?",
            file=sys.stderr,
        )
        return 2

    current = load_urls(current_file)
    previous = load_urls(args.previous / "urls.txt")
    allowed_missing: set[str] = set()
    if args.allow_missing.exists():
        allowed_missing = load_urls(args.allow_missing)

    if not previous:
        print(
            f"link-rot guard: no previous urls.txt at {args.previous}/urls.txt; "
            f"treating as first build (OK)",
            file=sys.stderr,
        )
        return 0

    missing = (previous - current) - allowed_missing
    if missing:
        print(
            f"\nlink-rot guard FAILED: {len(missing)} previously-published URL(s) "
            f"are not in the new build:",
            file=sys.stderr,
        )
        for u in sorted(missing):
            print(f"  - {u}", file=sys.stderr)
        print(
            "\nTo intentionally retire a URL, add it to "
            f"{args.allow_missing} with a PR comment explaining why.",
            file=sys.stderr,
        )
        return 1

    print(
        f"link-rot guard OK: {len(current)} URL(s) now published, "
        f"{len(previous)} previously, 0 missing"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
