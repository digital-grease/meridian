from drift_audit.storage.local import LocalSampleStore

__all__ = ["LocalSampleStore"]


def __getattr__(name: str):
    # Lazy re-export so `from drift_audit.storage import S3SampleUploader`
    # works without forcing boto3 to be importable at package load.
    if name in {"S3SampleUploader", "UploadReport", "maybe_build_uploader"}:
        from drift_audit.storage import s3
        return getattr(s3, name)
    raise AttributeError(name)
