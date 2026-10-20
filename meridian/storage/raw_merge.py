"""Merge a week's raw samples downloaded from S3 into the local raw tree.

run-weekly.sh pulls the week's raw from S3 before sampling so a fresh host
sees what already exists. It used to `aws s3 sync` straight into
data/raw/<week>/, and sync replaces any local file whose size differs. A
local file that had grown since the last upload (a run that sampled, then
died before uploading) was overwritten by the smaller S3 copy, and the
samples only that host had were gone. This happened to three files in the
2026-W42 top-up on 2026-10-20.

So the wrapper now downloads into an empty staging directory and calls
this module, which never replaces a local file that S3 does not fully
contain. Raw files are append-only JSONL, so "contains" is a byte prefix:

- local missing             -> copy from S3
- identical                 -> nothing
- S3 extends local          -> take S3 (another host sampled further)
- local extends S3          -> keep local (not uploaded yet)
- neither extends the other -> keep local and warn. The run's upload then
  replaces the S3 object, and bucket versioning keeps the old one.

Usage: python -m meridian.storage.raw_merge <staging_dir> <dest_dir>
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MergeReport:
    copied: list[str] = field(default_factory=list)
    identical: list[str] = field(default_factory=list)
    took_remote: list[str] = field(default_factory=list)
    kept_local: list[str] = field(default_factory=list)
    diverged: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.copied)} copied, {len(self.identical)} identical, "
            f"{len(self.took_remote)} extended from S3, "
            f"{len(self.kept_local)} kept local (ahead of S3), "
            f"{len(self.diverged)} diverged (kept local)"
        )


def merge_raw(staging: Path, dest: Path) -> MergeReport:
    report = MergeReport()
    for src in sorted(p for p in staging.rglob("*") if p.is_file()):
        rel = src.relative_to(staging)
        name = rel.as_posix()
        dst = dest / rel
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            report.copied.append(name)
            continue
        remote, local = src.read_bytes(), dst.read_bytes()
        if remote == local:
            report.identical.append(name)
        elif remote.startswith(local):
            # Write then rename, so a crash cannot leave a half-copied file
            # where a complete one was.
            tmp = dst.with_name(dst.name + ".merge-tmp")
            shutil.copy2(src, tmp)
            tmp.replace(dst)
            report.took_remote.append(name)
        elif local.startswith(remote):
            report.kept_local.append(name)
        else:
            report.diverged.append(name)
    return report


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m meridian.storage.raw_merge <staging_dir> <dest_dir>",
              file=sys.stderr)
        return 2
    staging, dest = Path(argv[0]), Path(argv[1])
    report = merge_raw(staging, dest)
    print(f"raw merge: {report.summary()}")
    for name in report.kept_local:
        print(f"  kept local, ahead of S3: {name}")
    for name in report.diverged:
        print(f"  WARN diverged, kept local: {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
