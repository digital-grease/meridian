"""Open Graph PNG emission tests.

Two branches:
  - matplotlib present → spot-check one PNG exists and starts with the
    PNG magic bytes; the build reports a non-zero emission count.
  - matplotlib absent → publish_og_images skips; the site still builds
    and templates fall back to the default SVG (assertion on rendered
    index.html).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _run_build(tmp_path: Path, extra_env: dict[str, str] | None = None) -> tuple[Path, str]:
    import os

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    dist = tmp_path / "dist"
    manifest = REPO_ROOT / "site" / "fixtures" / "synthetic-fixture.json"
    result = subprocess.run(
        [
            "uv", "run", "python",
            str(REPO_ROOT / "site" / "src" / "build.py"),
            "--manifest", str(manifest),
            "--out", str(dist),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env,
    )
    assert result.returncode == 0, (
        f"build failed: {result.stderr}\n{result.stdout}"
    )
    return dist, result.stdout


def test_og_pngs_written_when_matplotlib_available(tmp_path: Path):
    pytest.importorskip("matplotlib")

    dist, stdout = _run_build(tmp_path)
    og_dir = dist / "static" / "og"
    assert og_dir.is_dir(), "expected dist/static/og/ to be created"
    pngs = sorted(og_dir.glob("*.png"))
    assert pngs, "no OG PNGs emitted"
    assert any("emitted" in line and "OG PNG" in line for line in stdout.splitlines()), (
        f"expected build to report OG PNG count; stdout:\n{stdout}"
    )

    # Coverage spot-checks from the minimum-coverage set the plan requires.
    expected_any = {"index.png", "methodology.png"}
    present = {p.name for p in pngs}
    assert expected_any <= present, f"missing baseline PNGs: {expected_any - present}"

    # Magic bytes on one of them.
    sample = pngs[0]
    assert sample.read_bytes()[:8] == PNG_MAGIC, (
        f"{sample.name} is not a PNG (header: {sample.read_bytes()[:8]!r})"
    )

    # index.html references the per-page PNG, not the SVG fallback.
    index_html = (dist / "index.html").read_text()
    assert '/static/og/index.png' in index_html
    assert '/static/images/og-default.svg' not in index_html


def test_site_builds_when_matplotlib_unimportable(tmp_path: Path):
    """If matplotlib can't be imported, publish_og_images must degrade
    gracefully: no PNGs written, build still succeeds, templates fall
    back to og-default.svg.

    We shadow matplotlib itself via a PYTHONPATH-prepended shim package
    whose __init__.py raises ImportError. That shim takes priority over
    the real matplotlib, and site/src/og.py's try/except catches it.
    """
    shim_root = tmp_path / "py-shim"
    (shim_root / "matplotlib").mkdir(parents=True)
    (shim_root / "matplotlib" / "__init__.py").write_text(
        "raise ImportError('matplotlib shim raises (test)')\n"
    )

    sep = ";" if sys.platform == "win32" else ":"
    import os

    existing_pp = os.environ.get("PYTHONPATH", "")
    extra_env = {
        "PYTHONPATH": f"{shim_root}{sep}{existing_pp}" if existing_pp else str(shim_root),
    }

    dist, stdout_and_stderr = _run_build(tmp_path, extra_env=extra_env)
    og_dir = dist / "static" / "og"
    files = list(og_dir.iterdir()) if og_dir.exists() else []
    assert not files, (
        f"expected no PNGs when matplotlib is unavailable; found: {files}"
    )
    # Build still succeeded; index.html falls back to the default SVG.
    index_html = (dist / "index.html").read_text()
    assert '/static/images/og-default.svg' in index_html
    assert '/static/og/index.png' not in index_html
