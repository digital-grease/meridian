"""Safety heuristic on the site builder's out-dir wipe.

The builder wipes `out_dir` before rendering so stale pages from prior
builds can't pollute `sitemap.xml` / `urls.txt` / the link-rot guard.
A previous bug: a renamed model (`gpt-5-preview` → `gpt-5.1`) left the
old directory in place and the sitemap carried both.

The wipe is guarded — it only fires when the directory is empty or
carries a `build.json` marker left by a previous meridian build. This
test pins that contract so the guard can't silently regress.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SITE_SRC = REPO_ROOT / "site" / "src"
if str(SITE_SRC) not in sys.path:
    sys.path.insert(0, str(SITE_SRC))

from build import _safe_clean_dist  # type: ignore[import-not-found]  # noqa: E402


def test_safe_clean_missing_dir_is_noop(tmp_path: Path):
    target = tmp_path / "dist"
    _safe_clean_dist(target)
    assert not target.exists()


def test_safe_clean_empty_dir_is_noop(tmp_path: Path):
    target = tmp_path / "dist"
    target.mkdir()
    _safe_clean_dist(target)
    assert target.exists()
    assert list(target.iterdir()) == []


def test_safe_clean_wipes_dir_with_build_marker(tmp_path: Path):
    target = tmp_path / "dist"
    target.mkdir()
    (target / "build.json").write_text(json.dumps({"git_sha": "abc"}))
    (target / "index.html").write_text("<html>old</html>")
    (target / "models" / "gpt-5-preview").mkdir(parents=True)
    (target / "models" / "gpt-5-preview" / "index.html").write_text("stale")

    _safe_clean_dist(target)

    assert not target.exists(), "expected the whole directory to be removed"


def test_safe_clean_refuses_unmarked_nonempty_dir(tmp_path: Path):
    target = tmp_path / "not-a-build"
    target.mkdir()
    (target / "important.txt").write_text("please don't delete me")

    with pytest.raises(SystemExit, match="refusing to wipe"):
        _safe_clean_dist(target)

    assert (target / "important.txt").exists()
