"""S3 and metadata helper utilities used by API routes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    from botocore.exceptions import ClientError
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    ClientError = Exception  # type: ignore[assignment]
from fastapi import HTTPException

from pdf_analysis.api.constants import DEFAULT_TEMPLATE_PREFIXES

logger = logging.getLogger(__name__)


def _metadata_json_key(key: str) -> str:
    """Return the metadata sidecar key for a given S3 object key."""
    return f"{key}.meta.json"


def _update_object_metadata(
    s3_client: Any,
    bucket: str,
    key: str,
    version_id: Optional[str],
    metadata_fields: Dict[str, Any],
) -> None:
    """Merge generated metadata back onto the original S3 object."""
    try:
        head_kwargs: Dict[str, Any] = {"Bucket": bucket, "Key": key}
        if version_id:
            head_kwargs["VersionId"] = version_id
        head = s3_client.head_object(**head_kwargs)
        existing_metadata = head.get("Metadata", {})
        content_type = head.get("ContentType")
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Failed to read metadata for %s: %s", key, exc)
        existing_metadata = {}
        content_type = None

    new_metadata = dict(existing_metadata)
    labels = metadata_fields.get("labels")
    keywords = metadata_fields.get("keywords")
    language = metadata_fields.get("language")
    if labels:
        new_metadata["labels"] = ",".join(labels)
    if keywords:
        new_metadata["keywords"] = ",".join(keywords)
    if language:
        new_metadata["language"] = str(language)
    document_type = metadata_fields.get("ind_document_type")
    section_number = metadata_fields.get("ind_section_number")
    section_title = metadata_fields.get("ind_section_title")
    classification_confidence = metadata_fields.get("ind_classification_confidence")
    if document_type:
        new_metadata["ind_document_type"] = str(document_type)
    if section_number:
        new_metadata["ind_section_number"] = str(section_number)
    if section_title:
        new_metadata["ind_section_title"] = str(section_title)
    if classification_confidence is not None:
        new_metadata["ind_classification_confidence"] = str(classification_confidence)
    new_metadata["analyzed"] = "true"

    copy_source: Dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id:
        copy_source["VersionId"] = version_id

    copy_kwargs: Dict[str, Any] = {
        "Bucket": bucket,
        "Key": key,
        "CopySource": copy_source,
        "MetadataDirective": "REPLACE",
        "TaggingDirective": "COPY",
        "Metadata": new_metadata,
    }
    if content_type:
        copy_kwargs["ContentType"] = content_type

    try:
        s3_client.copy_object(**copy_kwargs)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Failed to persist metadata for %s: %s", key, exc)


def _upload_metadata_json_to_s3(
    s3_client: Any,
    bucket: str,
    key: str,
    payload: Dict[str, Any],
) -> None:
    """Persist a JSON metadata sidecar next to the original object."""
    meta_key = _metadata_json_key(key)
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=meta_key,
            Body=json.dumps(payload, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Failed to upload metadata json for %s: %s", meta_key, exc)


def _analysis_json_key(key: str) -> str:
    """Return the analysis sidecar key for a given S3 object key."""
    return f"{key}.analysis.json"


def _read_s3_text(s3_client: Any, bucket: str, key: str) -> Optional[str]:
    """Read s3 text."""
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
    except ClientError:
        return None
    body = obj.get("Body")
    if not body:
        return None
    return body.read().decode("utf-8")


def _normalize_extra_attributes(value: Any) -> Dict[str, Any]:
    """Normalize extra attributes."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _upload_analysis_json_to_s3(
    s3_client: Any,
    bucket: str,
    key: str,
    payload: Dict[str, Any],
) -> str:
    """Persist the richer analysis payload adjacent to the PDF."""
    analysis_key = _analysis_json_key(key)
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=analysis_key,
            Body=json.dumps(payload, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning(
            "Failed to upload analysis payload for %s: %s", analysis_key, exc
        )
    return analysis_key


def _boto3_client(service: str, region_name: Optional[str] = None) -> Any:
    """Boto3 client."""
    try:
        import importlib

        boto3_module = importlib.import_module("boto3")
    except ModuleNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail="boto3 is required for S3 operations",
        ) from exc
    return boto3_module.client(service, region_name=region_name)


def _normalize_prefix_list(prefixes: Optional[str]) -> List[str]:
    """Normalize prefix list."""
    if not prefixes:
        return list(DEFAULT_TEMPLATE_PREFIXES)
    normalized: List[str] = []
    for raw in prefixes.split(","):
        cleaned = raw.strip().lstrip("/")
        if not cleaned:
            continue
        if not cleaned.endswith("/"):
            cleaned = f"{cleaned}/"
        normalized.append(cleaned)
    return normalized or list(DEFAULT_TEMPLATE_PREFIXES)


def _list_template_objects(
    s3_client: Any,
    bucket: str,
    prefixes: Sequence[str],
) -> List[Dict[str, Any]]:
    """List template objects."""
    paginator = s3_client.get_paginator("list_objects_v2")
    objects: Dict[str, Dict[str, Any]] = {}
    for prefix in prefixes:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key")
                if not key or not key.lower().endswith(".docx"):
                    continue
                objects[key] = obj
    return [objects[key] for key in sorted(objects.keys())]


def _normalize_s3_prefix(prefix: str) -> str:
    """Normalize s3 prefix."""
    cleaned = prefix.strip().lstrip("/")
    if cleaned and not cleaned.endswith("/"):
        cleaned = f"{cleaned}/"
    return cleaned


def _normalize_s3_path_segment(value: str, field_name: str) -> str:
    """Normalize s3 path segment."""
    cleaned = value.strip().strip("/")
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    return cleaned


def _s3_folder_template_path() -> Path:
    """S3 folder template path."""
    config_dir = Path(__file__).resolve().parents[3] / "config"
    primary = config_dir / "s3_folder_template.json"
    if primary.exists():
        return primary
    fallback = config_dir / "s3_folder_templates.json"
    if fallback.exists():
        return fallback
    raise FileNotFoundError(
        f"Missing template file: {primary} (fallback checked: {fallback})"
    )


def _load_s3_folder_template() -> Dict[str, Any]:
    """Load s3 folder template."""
    template_path = _s3_folder_template_path()
    payload = json.loads(template_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("S3 folder template must be a JSON object")
    return payload


def _collect_s3_folder_keys(base_prefix: str, structure: Dict[str, Any]) -> List[str]:
    """Collect s3 folder keys."""
    keys: List[str] = []
    for folder_name, subfolders in structure.items():
        normalized_name = str(folder_name).strip().strip("/")
        if not normalized_name:
            continue
        prefix = f"{base_prefix}{normalized_name}/"
        keys.append(prefix)
        if isinstance(subfolders, dict) and subfolders:
            keys.extend(_collect_s3_folder_keys(prefix, subfolders))
    return keys


def _s3_prefix_exists(s3_client: Any, bucket: str, prefix: str) -> bool:
    """S3 prefix exists."""
    response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return bool(response.get("Contents"))


def _list_docx_objects(
    s3_client: Any,
    bucket: str,
    prefix: str,
    *,
    direct_only: bool = False,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """List docx objects."""
    paginator = s3_client.get_paginator("list_objects_v2")
    params: Dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
    if direct_only:
        params["Delimiter"] = "/"
    objects: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    for page in paginator.paginate(**params):
        for obj in page.get("Contents", []):
            key = obj.get("Key")
            if not key or not key.lower().endswith(".docx"):
                continue
            if key in seen_keys:
                continue
            seen_keys.add(key)
            objects.append(obj)
            if limit and len(objects) >= limit:
                return objects
    return objects
