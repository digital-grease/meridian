"""Redirect-emitter tests.

The hard rule is that published URLs must never 404. The redirect map
at ``site/redirects.yaml`` is the mechanism for honouring that rule
across renames; these tests are its contract.

The second half of this file covers redirect *targets*. Emitting a
redirect page is only half the guarantee: a redirect onto a path that
was never built serves a 200 that lands the reader on a 404, and until
2026-08-15 nothing in CI could see it, because the link-rot guard
computed only ``previous_urls - current_urls`` and the redirect page
itself never went missing.
``/models/gpt-5-preview/2026-W16/ -> /models/gpt-5.1/2026-W16/`` sat in
that blind spot from launch, its target having only ever existed on the
pre-launch fixture site.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_linkrot import find_dangling_redirects  # noqa: E402


def _build(tmp_path: Path, redirects_yaml: str) -> subprocess.CompletedProcess:
    """Run site/src/build.py with a scripted redirect map.

    The map goes in tmp_path and reaches the build through
    MERIDIAN_REDIRECTS. These tests used to swap the real
    site/redirects.yaml in place and restore it in a finally block, on
    the reasoning that a CLI flag just for tests would dilute the
    production contract. The env override keeps that property, since
    production never sets it, without the restore step: on 2026-08-15
    the identical pattern applied to data/run_log.jsonl destroyed 15 of
    its 17 entries when two pytest processes overlapped and the second
    restored the first's scripted file. Losing a restore here would
    silently drop a redirect row, and every row is a published URL that
    would begin serving a 404.
    """
    redirects = tmp_path / "redirects.yaml"
    redirects.write_text(redirects_yaml)
    manifest = REPO_ROOT / "site" / "fixtures" / "synthetic-fixture.json"
    return subprocess.run(
        [
            "uv", "run", "python",
            str(REPO_ROOT / "site" / "src" / "build.py"),
            "--manifest", str(manifest),
            "--out", str(tmp_path / "dist"),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
        env={**os.environ, "MERIDIAN_REDIRECTS": str(redirects)},
    )


def _run_build_with_redirects(tmp_path: Path, redirects_yaml: str) -> Path:
    """``_build`` for the cases that expect success. Returns dist."""
    result = _build(tmp_path, redirects_yaml)
    assert result.returncode == 0, (
        f"build failed: {result.stderr}\n{result.stdout}"
    )
    return tmp_path / "dist"


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
    result = _build(
        tmp_path,
        "redirects:\n"
        "  - from: /loop/\n"
        "    to:   /loop/\n",
    )
    assert result.returncode != 0
    assert "self-redirect" in (result.stderr + result.stdout)


def test_relative_paths_rejected(tmp_path: Path):
    result = _build(
        tmp_path,
        "redirects:\n"
        "  - from: old-path/\n"
        "    to:   /new-path/\n",
    )
    assert result.returncode != 0
    assert "site-root-relative" in (result.stderr + result.stdout)


# ---------------------------------------------------------------------
# Redirect targets must resolve in the build.
# ---------------------------------------------------------------------


def _write_redirects(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "redirects.yaml"
    p.write_text(body)
    return p


def test_dangling_target_is_reported(tmp_path: Path):
    """The exact shape of the bug that shipped: a live `from`, a `to`
    that the build never produced."""
    redirects = _write_redirects(
        tmp_path,
        "redirects:\n"
        "  - from: /models/gpt-5-preview/2026-W16/\n"
        "    to:   /models/gpt-5.1/2026-W16/\n"
        "    reason: \"renamed\"\n",
    )
    current = {"/", "/models/", "/models/gpt-5.1/", "/models/gpt-5-preview/2026-W16/"}
    assert find_dangling_redirects(redirects, current) == [
        ("/models/gpt-5-preview/2026-W16/", "/models/gpt-5.1/2026-W16/")
    ]


def test_resolvable_target_is_not_reported(tmp_path: Path):
    redirects = _write_redirects(
        tmp_path,
        "redirects:\n"
        "  - from: /models/gpt-5-preview/2026-W16/\n"
        "    to:   /models/gpt-5.1/\n",
    )
    current = {"/", "/models/gpt-5.1/"}
    assert find_dangling_redirects(redirects, current) == []


def test_file_target_resolves_by_exact_path(tmp_path: Path):
    """urls.txt carries file pages with their extension and directory
    pages with a trailing slash; both forms must match verbatim."""
    redirects = _write_redirects(
        tmp_path,
        "redirects:\n"
        "  - from: /old.html\n"
        "    to:   /404.html\n"
        "  - from: /older/\n"
        "    to:   /404\n",
    )
    current = {"/404.html"}
    assert find_dangling_redirects(redirects, current) == [("/older/", "/404")]


def test_chained_redirect_resolves(tmp_path: Path):
    """A redirect may point at another redirect: redirect pages land in
    urls.txt like any other page."""
    redirects = _write_redirects(
        tmp_path,
        "redirects:\n"
        "  - from: /a/\n"
        "    to:   /b/\n"
        "  - from: /b/\n"
        "    to:   /c/\n",
    )
    current = {"/b/", "/c/"}
    assert find_dangling_redirects(redirects, current) == []


def test_missing_redirects_file_is_not_an_error(tmp_path: Path):
    assert find_dangling_redirects(tmp_path / "nope.yaml", {"/"}) == []


def test_empty_redirects_file_is_not_an_error(tmp_path: Path):
    redirects = _write_redirects(tmp_path, "redirects: []\n")
    assert find_dangling_redirects(redirects, {"/"}) == []


def _run_linkrot(current_dir: Path, previous_dir: Path, redirects: Path):
    return subprocess.run(
        [
            "uv", "run", "python",
            str(REPO_ROOT / "scripts" / "check_linkrot.py"),
            "--current", str(current_dir),
            "--previous", str(previous_dir),
            "--redirects", str(redirects),
            "--allow-missing", str(REPO_ROOT / ".allow-missing-urls"),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )


def test_check_linkrot_exits_nonzero_on_dangling_target(tmp_path: Path):
    current = tmp_path / "current"
    current.mkdir()
    (current / "urls.txt").write_text("/\n/models/gpt-5.1/\n")
    previous = tmp_path / "previous"
    previous.mkdir()
    (previous / "urls.txt").write_text("/\n")
    redirects = _write_redirects(
        tmp_path,
        "redirects:\n"
        "  - from: /gone/\n"
        "    to:   /never-built/\n",
    )
    result = _run_linkrot(current, previous, redirects)
    assert result.returncode == 1, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "/never-built/" in combined
    assert "did not produce" in combined


def test_check_linkrot_checks_targets_even_without_a_previous_build(tmp_path: Path):
    """The first-deploy early return must not skip the target check: a
    build with no predecessor can still ship a dangling redirect."""
    current = tmp_path / "current"
    current.mkdir()
    (current / "urls.txt").write_text("/\n")
    previous = tmp_path / "previous"
    previous.mkdir()  # no urls.txt at all
    redirects = _write_redirects(
        tmp_path,
        "redirects:\n"
        "  - from: /gone/\n"
        "    to:   /never-built/\n",
    )
    result = _run_linkrot(current, previous, redirects)
    assert result.returncode == 1, result.stdout + result.stderr


def test_check_linkrot_passes_when_every_target_resolves(tmp_path: Path):
    current = tmp_path / "current"
    current.mkdir()
    (current / "urls.txt").write_text("/\n/models/gpt-5.1/\n")
    previous = tmp_path / "previous"
    previous.mkdir()
    (previous / "urls.txt").write_text("/\n")
    redirects = _write_redirects(
        tmp_path,
        "redirects:\n"
        "  - from: /models/gpt-5-preview/\n"
        "    to:   /models/gpt-5.1/\n",
    )
    result = _run_linkrot(current, previous, redirects)
    assert result.returncode == 0, result.stdout + result.stderr


def test_repo_redirects_resolve_against_a_real_build(tmp_path: Path):
    """The regression test proper: build the site from the newest real
    manifest and assert that every row in the checked-in
    site/redirects.yaml points at a page that build produced.

    Uses a real manifest rather than the synthetic fixture because the
    synthetic fixture's model roster is not the production one, and the
    redirect map is written against production model ids.

    The map is snapshotted into tmp_path and the check reads the copy.
    Nothing in this file writes to the real map any more, so the copy is
    belt and braces rather than a fix: it means a future test that
    reintroduces in-place swapping cannot make this one quietly validate
    a two-line fixture and report a pass for a map it never read. This
    is the only test here whose assertion depends on that file's
    *content*.
    """
    redirects_snapshot = tmp_path / "redirects.yaml"
    real_redirects = REPO_ROOT / "site" / "redirects.yaml"
    redirects_snapshot.write_text(
        real_redirects.read_text() if real_redirects.exists() else "redirects: []\n"
    )

    manifests = sorted(
        (REPO_ROOT / "site" / "fixtures").glob("manifest-*.json")
    )
    assert manifests, "no site/fixtures/manifest-*.json to build from"
    dist = tmp_path / "dist"
    result = subprocess.run(
        [
            "uv", "run", "python",
            str(REPO_ROOT / "site" / "src" / "build.py"),
            "--manifest", str(manifests[-1]),
            "--out", str(dist),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"build failed: {result.stderr}\n{result.stdout}"

    urls = {
        line.strip()
        for line in (dist / "urls.txt").read_text().splitlines()
        if line.strip()
    }
    dangling = find_dangling_redirects(redirects_snapshot, urls)
    assert not dangling, (
        "site/redirects.yaml points at paths this build did not produce; "
        f"each serves a 200 onto a 404: {dangling}"
    )
