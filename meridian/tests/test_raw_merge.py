"""raw_merge must never replace a local raw file that S3 does not contain."""
from __future__ import annotations

from pathlib import Path

from meridian.storage.raw_merge import main, merge_raw

REL = "claude-opus-5/pol-immigration-pathway/samples.jsonl"
A = b'{"i":0}\n'
B = b'{"i":1}\n'
C = b'{"i":2}\n'


def _write(root: Path, rel: str, data: bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_local_ahead_of_s3_is_kept(tmp_path):
    # The 2026-W42 case: the host sampled more, then died before uploading.
    staging, dest = tmp_path / "s", tmp_path / "d"
    _write(staging, REL, A)
    local = _write(dest, REL, A + B + C)
    report = merge_raw(staging, dest)
    assert local.read_bytes() == A + B + C
    assert report.kept_local == [REL]


def test_missing_local_is_copied(tmp_path):
    staging, dest = tmp_path / "s", tmp_path / "d"
    _write(staging, REL, A + B)
    report = merge_raw(staging, dest)
    assert (dest / REL).read_bytes() == A + B
    assert report.copied == [REL]


def test_s3_ahead_of_local_is_taken(tmp_path):
    staging, dest = tmp_path / "s", tmp_path / "d"
    _write(staging, REL, A + B)
    local = _write(dest, REL, A)
    report = merge_raw(staging, dest)
    assert local.read_bytes() == A + B
    assert report.took_remote == [REL]
    assert not list(dest.rglob("*.merge-tmp"))


def test_identical_is_untouched(tmp_path):
    staging, dest = tmp_path / "s", tmp_path / "d"
    _write(staging, REL, A)
    _write(dest, REL, A)
    assert merge_raw(staging, dest).identical == [REL]


def test_diverged_keeps_local(tmp_path):
    staging, dest = tmp_path / "s", tmp_path / "d"
    _write(staging, REL, A + B)
    local = _write(dest, REL, A + C)
    report = merge_raw(staging, dest)
    assert local.read_bytes() == A + C
    assert report.diverged == [REL]


def test_local_only_files_are_left_alone(tmp_path):
    staging, dest = tmp_path / "s", tmp_path / "d"
    staging.mkdir()
    other = _write(dest, "llama3.2:3b/neut-photosynthesis/samples.jsonl", A)
    merge_raw(staging, dest)
    assert other.read_bytes() == A


def test_cli_reports_and_exits_zero(tmp_path, capsys):
    staging, dest = tmp_path / "s", tmp_path / "d"
    _write(staging, REL, A)
    _write(dest, REL, A + B)
    assert main([str(staging), str(dest)]) == 0
    out = capsys.readouterr().out
    assert "1 kept local" in out
    assert REL in out
