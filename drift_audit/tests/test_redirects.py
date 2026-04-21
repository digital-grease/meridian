"""Redirect-emitter tests.

The hard rule is that published URLs must never 404. The redirect map
at ``site/redirects.yaml`` is the mechanism for honouring that rule
across renames; these tests are its contract.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _run_build_with_redirects(
    tmp_path: Path,
    redirects_yaml: str,
) -> Path:
    """Run site/src/build.py against the repo's current fixture but with
    a temporarily-swapped redirects.yaml. Returns the dist directory.

    We swap the redirects file in-place rather than adding a CLI flag
    because the build already reads the canonical path and adding a
    flag just for tests would dilute the production contract.
    """
    redirects_path = REPO_ROOT / "site" / "redirects.yaml"
    backup = redirects_path.read_text() if redirects_path.exists() else None
    redirects_path.write_text(redirects_yaml)
    try:
        dist = tmp_path / "dist"
        manifest = REPO_ROOT / "site" / "fixtures" / "manifest-2026-W16.json"
        result = subprocess.run(
            [
                "uv", "run", "python",
                str(REPO_ROOT / "site" / "src" / "build.py"),
                "--manifest", str(manifest),
                "--out", str(dist),
            ],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert result.returncode == 0, (
            f"build failed: {result.stderr}\n{result.stdout}"
        )
        return dist
    finally:
        if backup is not None:
            redirects_path.write_text(backup)
        else:
            redirects_path.unlink()


def test_empty_redirects_is_noop(tmp_path: Path):
    dist = _run_build_with_redirects(
        tmp_path, "redirects: []\n",
    )
    # Build succeeded and the usual URLs all exist; nothing extra emitted.
    urls = (dist / "urls.txt").read_text().splitlines()
    # Sanity: a known page exists; no stray redirect-only path appears.
    assert "/" in urls
    assert "/moved-somewhere/" not in urls


def test_directory_redirect_emits_meta_refresh_and_visible_link(tmp_path: Path):
    yaml_text = (
        "redirects:\n"
        "  - from: /old-methodology/\n"
        "    to:   /methodology/\n"
        "    reason: \"renamed in 2026-04 dashboard refresh\"\n"
    )
    dist = _run_build_with_redirects(tmp_path, yaml_text)

    emitted = dist / "old-methodology" / "index.html"
    assert emitted.exists(), "redirect page was not written"
    body = emitted.read_text()
    assert 'http-equiv="refresh"' in body
    assert 'url=/methodology/' in body
    assert 'rel="canonical"' in body
    assert 'href="/methodology/"' in body
    # Visible fallback for users whose browsers block meta-refresh.
    assert "This page has moved" in body
    assert "renamed in 2026-04 dashboard refresh" in body

    # Redirect URL participates in urls.txt so link-rot sees it.
    urls = set((dist / "urls.txt").read_text().splitlines())
    assert "/old-methodology/" in urls


def test_file_redirect_emits_at_exact_path(tmp_path: Path):
    """A redirect whose `from` is a file (has a suffix) lands at that
    exact path, not under an extra index.html."""
    yaml_text = (
        "redirects:\n"
        "  - from: /old.html\n"
        "    to:   /about/\n"
        "    reason: \"legacy file path\"\n"
    )
    dist = _run_build_with_redirects(tmp_path, yaml_text)
    assert (dist / "old.html").exists()
    assert not (dist / "old.html" / "index.html").exists()


def test_self_redirect_is_rejected(tmp_path: Path):
    yaml_text = (
        "redirects:\n"
        "  - from: /loop/\n"
        "    to:   /loop/\n"
    )
    redirects_path = REPO_ROOT / "site" / "redirects.yaml"
    backup = redirects_path.read_text() if redirects_path.exists() else None
    redirects_path.write_text(yaml_text)
    try:
        manifest = REPO_ROOT / "site" / "fixtures" / "manifest-2026-W16.json"
        result = subprocess.run(
            [
                "uv", "run", "python",
                str(REPO_ROOT / "site" / "src" / "build.py"),
                "--manifest", str(manifest),
                "--out", str(tmp_path / "dist"),
            ],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert result.returncode != 0
        assert "self-redirect" in (result.stderr + result.stdout)
    finally:
        if backup is not None:
            redirects_path.write_text(backup)
        else:
            redirects_path.unlink()


def test_relative_paths_rejected(tmp_path: Path):
    yaml_text = (
        "redirects:\n"
        "  - from: old-path/\n"
        "    to:   /new-path/\n"
    )
    redirects_path = REPO_ROOT / "site" / "redirects.yaml"
    backup = redirects_path.read_text() if redirects_path.exists() else None
    redirects_path.write_text(yaml_text)
    try:
        manifest = REPO_ROOT / "site" / "fixtures" / "manifest-2026-W16.json"
        result = subprocess.run(
            [
                "uv", "run", "python",
                str(REPO_ROOT / "site" / "src" / "build.py"),
                "--manifest", str(manifest),
                "--out", str(tmp_path / "dist"),
            ],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert result.returncode != 0
        assert "site-root-relative" in (result.stderr + result.stdout)
    finally:
        if backup is not None:
            redirects_path.write_text(backup)
        else:
            redirects_path.unlink()
