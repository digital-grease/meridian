#!/usr/bin/env python3
"""Link-rot guard for the Meridian site.

Two checks, both enforcing the same hard project guarantee: a published
URL must never 404.

1. **Disappearance.** Compare URL sets between the new build and a
   previous build snapshot, and fail if any previously-published URL is
   missing from the new one. If a URL needs to be retired deliberately
   (extremely rare), add it to the allowlist file to acknowledge the
   breakage in review.

2. **Dangling redirect targets.** Every ``to:`` in
   ``site/redirects.yaml`` must resolve to a path the new build
   actually produced.

Check 2 was added 2026-08-15. Check 1 computes only
``previous_urls - current_urls``, which makes a redirect onto a URL
that was never built structurally invisible: the redirect page itself
is emitted, so it appears in both URL sets and never goes missing, and
the guard passes while the reader gets a 200 that meta-refreshes onto a
hard 404. That is exactly what
``/models/gpt-5-preview/2026-W16/ -> /models/gpt-5.1/2026-W16/`` did
from launch until it was caught by hand, the target having only ever
existed on the pre-launch fixture site. Nothing in CI could have found
it, which is the real defect: the class stays closed only if the guard
resolves targets rather than just diffing sets.

URLs are read from ``urls.txt`` files produced by ``build.py``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

#: Repo root, derived from this file's own location rather than from the
#: working directory. The redirect map lives at a fixed place in the
#: repo, and a cwd-relative default silently resolved to nothing when the
#: guard ran from anywhere else: ``load_redirect_targets`` returns no
#: pairs for a file that is not there, so the check passed with zero
#: targets examined. A guard whose entire justification is that the
#: previous one could not see this bug class must not have a no-op
#: default.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REDIRECTS = REPO_ROOT / "site" / "redirects.yaml"


def load_urls(urls_file: Path) -> set[str]:
    if not urls_file.exists():
        return set()
    return {
        line.strip()
        for line in urls_file.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def load_redirect_targets(redirects_file: Path) -> list[tuple[str, str]]:
    """``(from, to)`` pairs from ``site/redirects.yaml``, in file order.

    A missing or empty file yields no pairs. Malformed rows (no ``to``)
    are skipped here rather than raised on: ``build.py`` is the module
    that owns validating redirect syntax, and it fails the build loudly
    on bad rows before this guard ever runs.
    """
    if not redirects_file.exists():
        return []
    raw = yaml.safe_load(redirects_file.read_text()) or {}
    pairs: list[tuple[str, str]] = []
    for entry in raw.get("redirects") or []:
        if not isinstance(entry, dict):
            continue
        src, dst = entry.get("from"), entry.get("to")
        if not isinstance(src, str) or not isinstance(dst, str):
            continue
        pairs.append((src.strip(), dst.strip()))
    return pairs


def find_dangling_redirects(
    redirects_file: Path, current: set[str]
) -> list[tuple[str, str]]:
    """``(from, to)`` pairs whose target is absent from the new build.

    ``current`` is the set of site-root-relative paths in the build's
    ``urls.txt``, where a directory-indexed page appears with its
    trailing slash (``/models/gpt-5.1/``) and a file page appears with
    its extension (``/404.html``). A target is resolvable when it
    appears verbatim. Redirect pages are themselves in ``urls.txt``, so
    a redirect chain resolves too.
    """
    return [
        (src, dst)
        for src, dst in load_redirect_targets(redirects_file)
        if dst not in current
    ]


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
    p.add_argument(
        "--redirects",
        type=Path,
        default=DEFAULT_REDIRECTS,
        help=(
            "Redirect map whose `to:` targets must resolve in the new build. "
            f"Defaults to {DEFAULT_REDIRECTS}. Absent file = no redirects "
            "to check, reported as a warning rather than a pass."
        ),
    )
    args = p.parse_args(argv)

    if not args.redirects.exists():
        # Not fatal: a checkout with no redirect map genuinely has no
        # targets to resolve. But it is reported, because "0 redirects
        # checked" and "0 redirects dangling" look identical in a green
        # CI log and only one of them means the guard ran.
        print(
            f"WARNING: no redirect map at {args.redirects}; the dangling-target "
            f"check has nothing to examine. Pass --redirects if the map lives "
            f"elsewhere.",
            file=sys.stderr,
        )

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

    # Redirect targets are checked first, and against the current build
    # alone. This check needs no previous build, so running it before the
    # first-build early-return below means a dangling target is caught
    # even on a deploy where the disappearance check cannot run.
    dangling = find_dangling_redirects(args.redirects, current)
    if dangling:
        print(
            f"\nlink-rot guard FAILED: {len(dangling)} redirect(s) in "
            f"{args.redirects} point at a path this build did not produce. "
            f"Each one serves a 200 that lands the reader on a 404:",
            file=sys.stderr,
        )
        for src, dst in dangling:
            print(f"  - {src}  ->  {dst}   (target not in urls.txt)", file=sys.stderr)
        print(
            "\nRepoint the redirect at a page that exists. Retiring the "
            "`from` path instead re-breaks a URL that is currently served, "
            "so it needs an explicit "
            f"{args.allow_missing} entry as well.",
            file=sys.stderr,
        )
        return 1

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
        f"{len(previous)} previously, 0 missing; "
        f"{len(load_redirect_targets(args.redirects))} redirect target(s) resolve"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
