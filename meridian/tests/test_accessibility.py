"""Accessibility (axe-core) contract test.

Runs axe-core 4.x against a local HTTP-served build and asserts zero
violations on the representative page list. The GitHub Actions workflow
at .github/workflows/weekly-build.yml is the runtime tripwire; this test
is the local fast-path so violations surface before push.

Marked @pytest.mark.slow: the first npx run downloads @axe-core/cli
(~20 s); subsequent runs are cached (~5 s for nine pages). Skipped
automatically when npx is not installed.

Note on file:// vs http://: templates reference /static/... with
root-relative paths. Under file:// those resolve to the filesystem root
and the stylesheet never loads — axe then evaluates Chromium UA defaults
and produces ~440 spurious color-contrast violations. We serve dist over
a local HTTP server so axe sees the real rendering.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import threading
from contextlib import closing
from functools import partial
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Representative pages: every distinct template in site/src/templates/.
# Static page paths exercise template types whose URLs are stable
# regardless of which manifest the site builds from. Per-item pages
# (a specific report / model / axis / prompt) used to be hardcoded
# here too — but those slugs shift as the corpus / roster / cadence
# change, so we now pick one of each from urls.txt at test time.
STATIC_PAGE_PATHS = [
    "/",
    "/about/",
    "/methodology/",
    "/reports/",
    "/data/",
    "/contribute/",
    "/funding/",
]
# Per-item template types to spot-check via dynamic pick.
#
# "/data/" picks a per-week snapshot page (data_week.html, or
# data_gap.html when the manifest's first week is one the audit lost).
# Both were unreachable from this list until 2026-08-15: STATIC_PAGE_PATHS
# covers only the /data/ index, so no /data/{week}/ page type was ever
# axe-gated, and those are the pages a journalist following a citation
# lands on.
DYNAMIC_PAGE_PREFIXES = ("/axes/", "/models/", "/prompts/", "/data/")


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args, **kwargs) -> None:  # noqa: D401
        return


@pytest.fixture(scope="module")
def built_dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    dist = tmp_path_factory.mktemp("axe-dist")
    manifest = REPO_ROOT / "site" / "fixtures" / "synthetic-fixture.json"
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
        f"site build failed: {result.stderr}\n{result.stdout}"
    )
    return dist


@pytest.fixture(scope="module")
def axe_server(built_dist: Path):
    port = _free_port()
    handler = partial(_QuietHandler, directory=str(built_dist))
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _pick_first_non_redirect(urls_txt: Path, prefix: str) -> str | None:
    """Return the first URL under ``prefix`` whose page is not a
    redirect stub (those meta-refresh before axe locks on)."""
    if not urls_txt.exists():
        return None
    dist = urls_txt.parent
    for line in urls_txt.read_text().splitlines():
        u = line.strip()
        if not u or not u.startswith(prefix) or u == prefix:
            continue
        # Strip leading "/" so it's a relative path under dist.
        rel = u.lstrip("/").rstrip("/")
        index_html = dist / rel / "index.html"
        if not index_html.exists():
            continue
        if 'http-equiv="refresh"' in index_html.read_text():
            continue
        return u
    return None


@pytest.mark.slow
def test_axe_no_violations(axe_server: str, built_dist: Path, tmp_path: Path) -> None:
    if shutil.which("npx") is None:
        pytest.skip("npx not available; CI workflow runs the hard gate")

    paths = list(STATIC_PAGE_PATHS)
    urls_txt = built_dist / "urls.txt"
    for prefix in DYNAMIC_PAGE_PREFIXES:
        picked = _pick_first_non_redirect(urls_txt, prefix)
        if picked is not None:
            paths.append(picked)

    urls = [axe_server + p for p in paths]
    # axe-core/cli 4.x resolves --save as cwd-relative and strips leading
    # slashes, so we run from tmp_path and pass a bare filename.
    out_json = tmp_path / "axe.json"
    # Point axe at the *system* chromedriver, mirroring what
    # weekly-build.yml does. Without this, axe falls back to the driver
    # bundled with @axe-core/cli, which tracks whatever Chrome was
    # current when that package was published — on a rolling distro it
    # is reliably behind the installed browser, and axe dies with
    # "session not created" before writing any JSON. CI carried this
    # workaround from the start; the test did not, so the local run of
    # the accessibility gate had been failing for an environmental
    # reason unrelated to accessibility.
    driver_flag: list[str] = []
    driver = os.environ.get("CHROMEDRIVER_PATH") or shutil.which("chromedriver")
    if driver:
        driver_flag = ["--chromedriver-path", driver]
    result = subprocess.run(
        [
            "npx", "--yes", "-p", "@axe-core/cli@4", "axe",
            *urls,
            *driver_flag,
            "--save", out_json.name,
        ],
        capture_output=True, text=True, cwd=tmp_path,
        timeout=300,
    )
    assert out_json.exists(), (
        "axe produced no JSON. "
        f"returncode={result.returncode}\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    data = json.loads(out_json.read_text())
    if isinstance(data, dict):
        data = [data]
    failures: list[str] = []
    for page in data:
        for v in page.get("violations", []):
            for n in v.get("nodes", []):
                failures.append(
                    f"{page['url']} — {v['id']} ({v['impact']}): {n['target']}"
                )
    assert not failures, "axe-core violations:\n  " + "\n  ".join(failures)
