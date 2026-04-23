"""S3 uploader tests using moto's in-process S3 mock.

Covers:
  - empty store → no uploads
  - week with multiple pairs → each samples.jsonl lands at the right key
  - repeat upload → skipped via ETag match
  - manifest upload writes both {week}.json and latest.json
  - uploader disabled via config absence → maybe_build_uploader returns None

Network is never contacted; ``moto.mock_aws`` patches boto3 in-process.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from meridian.config import S3StorageSpec
from meridian.runners.base import Sample
from meridian.storage import LocalSampleStore
from meridian.storage.s3 import S3SampleUploader, maybe_build_uploader

BUCKET = "meridian-test"
REGION = "us-east-1"


def _sample(prompt_id: str, model_id: str, idx: int, text: str) -> Sample:
    return Sample(
        prompt_id=prompt_id,
        model_id=model_id,
        provider="fake",
        request_index=idx,
        temperature=1.0,
        max_tokens=1024,
        text=text,
        model_version_string=f"{model_id}-2026-04-19",
        stop_reason="stop",
        latency_ms=1,
        captured_at=datetime(2026, 4, 19, tzinfo=timezone.utc),
    )


@pytest.fixture
def _s3_client():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield client


def _spec(prefix: str = "") -> S3StorageSpec:
    return S3StorageSpec(bucket=BUCKET, region=REGION, prefix=prefix)


def test_maybe_build_uploader_returns_none_when_spec_absent():
    assert maybe_build_uploader(None) is None


def test_upload_week_on_empty_store(tmp_path: Path, _s3_client):
    store = LocalSampleStore(tmp_path)
    uploader = S3SampleUploader(_spec(), client=_s3_client)

    report = uploader.upload_week(store, "2026-W16")

    assert report.files_uploaded == 0
    assert report.files_skipped == 0
    assert report.errors == []


def test_upload_week_mirrors_every_samples_jsonl(tmp_path: Path, _s3_client):
    store = LocalSampleStore(tmp_path)
    for prompt_id in ("p1", "p2", "p3"):
        for i in range(3):
            store.append(
                "2026-W16", "claude-opus-4-7", prompt_id,
                _sample(prompt_id, "claude-opus-4-7", i, "substantive answer"),
            )
    # Different week, should be ignored by upload_week("2026-W16")
    store.append(
        "2026-W15", "claude-opus-4-7", "p1",
        _sample("p1", "claude-opus-4-7", 0, "old"),
    )

    uploader = S3SampleUploader(_spec(prefix="meridian"), client=_s3_client)
    report = uploader.upload_week(store, "2026-W16")
    assert report.files_uploaded == 3
    assert report.files_skipped == 0
    assert report.bytes_uploaded > 0

    # Exact keys exist.
    listing = _s3_client.list_objects_v2(Bucket=BUCKET)
    keys = {obj["Key"] for obj in listing.get("Contents", [])}
    expected = {
        f"meridian/raw/2026-W16/claude-opus-4-7/{pid}/samples.jsonl"
        for pid in ("p1", "p2", "p3")
    }
    assert expected.issubset(keys)
    # The W15 file was not uploaded in this call.
    assert not any("/2026-W15/" in k for k in keys)


def test_repeat_upload_is_idempotent(tmp_path: Path, _s3_client):
    store = LocalSampleStore(tmp_path)
    for i in range(2):
        store.append(
            "2026-W16", "claude-opus-4-7", "p1",
            _sample("p1", "claude-opus-4-7", i, "same content"),
        )

    uploader = S3SampleUploader(_spec(), client=_s3_client)
    first = uploader.upload_week(store, "2026-W16")
    assert first.files_uploaded == 1
    assert first.files_skipped == 0

    second = uploader.upload_week(store, "2026-W16")
    assert second.files_uploaded == 0
    assert second.files_skipped == 1
    assert second.bytes_uploaded == 0


def test_upload_manifest_writes_both_keys(tmp_path: Path, _s3_client):
    manifest_path = tmp_path / "manifest-2026-W16.json"
    manifest_path.write_text('{"schema_version": 2}\n')

    uploader = S3SampleUploader(_spec(), client=_s3_client)
    report = uploader.upload_manifest(manifest_path, "2026-W16")
    assert report.files_uploaded == 2  # versioned + latest pointer
    assert report.errors == []

    keys = {obj["Key"] for obj in
            _s3_client.list_objects_v2(Bucket=BUCKET).get("Contents", [])}
    assert "manifests/2026-W16.json" in keys
    assert "manifests/latest.json" in keys


def test_upload_manifest_without_latest_pointer(tmp_path: Path, _s3_client):
    manifest_path = tmp_path / "m.json"
    manifest_path.write_text("{}\n")
    spec = S3StorageSpec(
        bucket=BUCKET, region=REGION, publish_latest_pointer=False,
    )
    uploader = S3SampleUploader(spec, client=_s3_client)
    report = uploader.upload_manifest(manifest_path, "2026-W16")
    assert report.files_uploaded == 1

    keys = {obj["Key"] for obj in
            _s3_client.list_objects_v2(Bucket=BUCKET).get("Contents", [])}
    assert "manifests/2026-W16.json" in keys
    assert "manifests/latest.json" not in keys


def test_cli_maybe_archive_to_s3_is_noop_without_config(tmp_path: Path, capsys):
    """CLI wiring: when config has no S3 section, no client is built and
    no output is produced."""
    from meridian.config import PipelineConfig
    from meridian.pipeline.cli import _maybe_archive_to_s3

    config = PipelineConfig()  # default: storage.s3 is None
    store = LocalSampleStore(tmp_path)
    _maybe_archive_to_s3(config, store, "2026-W16", tmp_path / "missing.json")
    assert capsys.readouterr().out == ""


def test_cli_maybe_archive_to_s3_uploads_when_enabled(
    tmp_path: Path, _s3_client, monkeypatch, capsys
):
    """When config.storage.s3 is set, _maybe_archive_to_s3 uploads raw
    samples and the public manifest. Test injects the moto-mocked client
    via the same constructor path the production code uses."""
    from meridian.config import PipelineConfig, S3StorageSpec, StorageSpec
    from meridian.pipeline import cli as cli_module

    store = LocalSampleStore(tmp_path / "raw")
    store.append(
        "2026-W16", "claude-opus-4-7", "p1",
        _sample("p1", "claude-opus-4-7", 0, "answer"),
    )
    manifest_path = tmp_path / "manifest-2026-W16.json"
    manifest_path.write_text('{"schema_version": 2}\n')

    config = PipelineConfig(
        storage=StorageSpec(
            raw_dir="raw",
            s3=S3StorageSpec(bucket=BUCKET, region=REGION, prefix="test"),
        ),
    )

    # Intercept uploader construction to use the moto client.
    def _build_with_mock(spec):
        if spec is None:
            return None
        return S3SampleUploader(spec, client=_s3_client)

    monkeypatch.setattr(cli_module, "maybe_build_uploader", _build_with_mock)
    cli_module._maybe_archive_to_s3(config, store, "2026-W16", manifest_path)

    out = capsys.readouterr().out
    assert "s3: raw samples" in out
    assert "s3: manifest" in out
    keys = {obj["Key"] for obj in
            _s3_client.list_objects_v2(Bucket=BUCKET).get("Contents", [])}
    assert "test/raw/2026-W16/claude-opus-4-7/p1/samples.jsonl" in keys
    assert "test/manifests/2026-W16.json" in keys


def test_upload_records_error_when_bucket_missing(tmp_path: Path):
    """Put against a non-existent bucket should be captured as an error,
    not raised. Pipeline continues even when durability archive fails."""
    store = LocalSampleStore(tmp_path)
    store.append(
        "2026-W16", "claude-opus-4-7", "p1",
        _sample("p1", "claude-opus-4-7", 0, "content"),
    )

    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        # Deliberately do NOT create the bucket.
        uploader = S3SampleUploader(
            S3StorageSpec(bucket="does-not-exist", region=REGION),
            client=client,
        )
        report = uploader.upload_week(store, "2026-W16")

    assert report.files_uploaded == 0
    assert report.errors, "expected at least one error when bucket is missing"
