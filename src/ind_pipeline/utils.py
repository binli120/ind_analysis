"""Utility helpers for S3 paths and timestamp formatting used by pipeline stages."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Tuple


def parse_s3_uri(uri: str) -> Tuple[str, str]:
    """
    Split an S3 URI (s3://bucket/key) into bucket and key components.
    """
    if not uri:
        raise ValueError("S3 URI is empty")
    prefix = "s3://"
    if not uri.startswith(prefix):
        raise ValueError(f"Unsupported S3 URI format: {uri}")
    remainder = uri[len(prefix):]
    bucket, _, key = remainder.partition("/")
    if not bucket or not key:
        raise ValueError(f"Malformed S3 URI: {uri}")
    return bucket, key


def build_analysis_key(object_key: str, suffix: str) -> str:
    """
    Derive an analysis artifact key relative to the original object key.
    Example:
        object_key='docs/file.pdf', suffix='analysis.json'
        -> 'analysis/docs/file.analysis.json'
    """
    if not object_key:
        raise ValueError("Object key is required to build analysis key")
    suffix_value = suffix.lstrip(".")
    source_path = PurePosixPath(object_key)
    try:
        base_path = source_path.with_suffix("")
    except ValueError:
        base_path = source_path
    target = PurePosixPath("analysis") / base_path
    return f"{target}.{suffix_value}"


def build_metadata_key(object_key: str, suffix: str) -> str:
    """
    Derive a metadata artifact key relative to the original object key.
    Placed under the 'metadata/' prefix to avoid clashing with analysis artifacts.
    """
    if not object_key:
        raise ValueError("Object key is required to build metadata key")
    suffix_value = suffix.lstrip(".")
    source_path = PurePosixPath(object_key)
    try:
        base_path = source_path.with_suffix("")
    except ValueError:
        base_path = source_path
    target = PurePosixPath("metadata") / base_path
    return f"{target}.{suffix_value}"


def utc_timestamp() -> str:
    """
    Return an ISO-8601 timestamp with UTC timezone designator.
    """
    return datetime.now(timezone.utc).isoformat()
