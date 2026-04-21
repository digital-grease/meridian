"""S3 archival of raw samples and published manifests.

Uploads are idempotent by ETag: objects already present with matching
content are skipped. Failures are recorded but never raised — the
pipeline treats S3 as a durable *mirror* of ``LocalSampleStore``, not as
the authoritative store. The local JSONL layout is what gets analyzed.

Configuration lives on :class:`drift_audit.config.S3StorageSpec`; when
absent, no uploader is constructed and the ``run`` command behaves as
it did before S3 was introduced.

Key layout under ``s3://{bucket}/{prefix}``:
  raw/{week_id}/{model_id}/{prompt_id}/samples.jsonl
  manifests/{week_id}.json
  manifests/latest.json        (optional; published pointer to most recent)

Credentials: never sourced from config or the repo. boto3's default
credential chain applies (env vars, instance profile, GitHub OIDC).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from drift_audit.config import S3StorageSpec
from drift_audit.storage import LocalSampleStore

_log = logging.getLogger(__name__)


@dataclass
class UploadReport:
    """Summary of one S3 upload cycle. Serialized into the run log."""
    files_uploaded: int = 0
    files_skipped: int = 0
    bytes_uploaded: int = 0
    errors: list[str] = field(default_factory=list)

    def pretty(self) -> str:
        parts = [
            f"uploaded {self.files_uploaded}",
            f"skipped {self.files_skipped}",
            f"{self.bytes_uploaded} bytes",
        ]
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts)


def _s3_key(prefix: str, *parts: str) -> str:
    """Join a configured prefix with path parts, canonicalising slashes."""
    clean = prefix.strip("/")
    joined = "/".join(p.strip("/") for p in parts if p)
    return f"{clean}/{joined}" if clean else joined


def _md5_hex(path: Path) -> str:
    """S3's ETag for a single-part upload is the MD5 hex of the body.

    Large files uploaded as multipart have composite ETags; we stay
    under the 5 GB single-part ceiling for raw JSONL, so the plain MD5
    equality check is sufficient for "already uploaded" detection.
    """
    h = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class S3SampleUploader:
    """Upload raw samples and manifests to S3.

    The ``client`` constructor parameter is optional so tests can inject
    a moto-mocked boto3 client. In production, ``None`` triggers the
    default boto3 session using the region on ``spec``.
    """

    def __init__(
        self,
        spec: S3StorageSpec,
        *,
        client: object | None = None,
    ) -> None:
        self.spec = spec
        if client is not None:
            self._client = client
        else:
            try:
                import boto3  # noqa: PLC0415
            except ImportError as e:
                raise RuntimeError(
                    "S3 upload requires the `storage-s3` dep group. "
                    "Install with: uv sync --group storage-s3"
                ) from e
            kwargs: dict[str, object] = {}
            if spec.region:
                kwargs["region_name"] = spec.region
            self._client = boto3.client("s3", **kwargs)

    def _already_uploaded(self, key: str, local_md5: str) -> bool:
        """Head the object and compare ETag; missing object returns False."""
        try:
            head = self._client.head_object(Bucket=self.spec.bucket, Key=key)
        except Exception:  # 404 or access issue — treat as "not there"
            return False
        etag = head.get("ETag", "").strip('"')
        # Multipart ETags contain a dash — never match a single-part MD5.
        return etag == local_md5

    def _put_file(
        self,
        path: Path,
        key: str,
        *,
        report: UploadReport,
        content_type: str = "application/x-ndjson",
    ) -> None:
        try:
            local_md5 = _md5_hex(path)
            if self._already_uploaded(key, local_md5):
                report.files_skipped += 1
                return
            with path.open("rb") as body:
                self._client.put_object(
                    Bucket=self.spec.bucket,
                    Key=key,
                    Body=body,
                    ContentType=content_type,
                    ContentMD5="",  # boto3 can compute on its own when needed
                )
            report.files_uploaded += 1
            report.bytes_uploaded += path.stat().st_size
        except Exception as e:  # pragma: no cover - integration failure path
            msg = f"{key}: {e}"
            _log.warning("s3 upload failed: %s", msg)
            report.errors.append(msg)

    def upload_week(
        self, store: LocalSampleStore, week_id: str
    ) -> UploadReport:
        """Mirror every stored ``samples.jsonl`` for ``week_id`` into S3."""
        report = UploadReport()
        week_dir = store.base_dir / week_id
        if not week_dir.exists():
            return report
        for samples_file in sorted(week_dir.rglob("samples.jsonl")):
            rel = samples_file.relative_to(store.base_dir)
            key = _s3_key(self.spec.prefix, "raw", rel.as_posix())
            self._put_file(samples_file, key, report=report)
        return report

    def upload_manifest(
        self, manifest_path: Path, week_id: str
    ) -> UploadReport:
        """Upload the public manifest to ``manifests/{week_id}.json`` and,
        when configured, also to ``manifests/latest.json`` as a pointer
        to the most recent published snapshot."""
        report = UploadReport()
        self._put_file(
            manifest_path,
            _s3_key(self.spec.prefix, "manifests", f"{week_id}.json"),
            report=report,
            content_type="application/json",
        )
        if self.spec.publish_latest_pointer:
            self._put_file(
                manifest_path,
                _s3_key(self.spec.prefix, "manifests", "latest.json"),
                report=report,
                content_type="application/json",
            )
        return report

    def upload_responses_snapshot(
        self, gzip_path: Path, week_id: str
    ) -> UploadReport:
        """Archive the public responses gzip under
        ``snapshots/{week_id}/responses.jsonl.gz``. Missing local file is
        a no-op (the emission step decides whether one exists)."""
        report = UploadReport()
        if not gzip_path.exists():
            return report
        self._put_file(
            gzip_path,
            _s3_key(self.spec.prefix, "snapshots", week_id, "responses.jsonl.gz"),
            report=report,
            content_type="application/gzip",
        )
        return report


def maybe_build_uploader(spec: S3StorageSpec | None) -> S3SampleUploader | None:
    """Construct an uploader when ``spec`` is present, else ``None``.

    Centralizes the "S3 is opt-in" branching so callers stay linear.
    """
    if spec is None:
        return None
    return S3SampleUploader(spec)
