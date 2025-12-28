# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""
@author: Bin Lee
@email: blee@filynai.com

Lambda entry point that records ingest events and tracks pipeline progress."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

try:
    import boto3
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    boto3 = None  # type: ignore[assignment]

from src.utils.utils import _utc_now, _extract_event_time

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb: Any | None = None
sns: Any | None = None

_TABLE_NAME = os.getenv("PIPELINE_STATUS_TABLE", "")
if not _TABLE_NAME:
    logger.warning(
        "PIPELINE_STATUS_TABLE is not set; handler will raise on first invocation."
    )

_DEFAULT_COMPANY = os.getenv("DEFAULT_COMPANY", "filynai.com")
_DEFAULT_PROJECT = os.getenv("DEFAULT_PROJECT", "LT1009")
_ALLOWED_EXTENSIONS = {
    ext.strip().lower()
    for ext in os.getenv("ALLOWED_EXTENSIONS", "pdf,doc,docx").split(",")
    if ext.strip()
}

_COMPLETION_TOPIC_ARN = os.getenv("INGEST_COMPLETED_TOPIC_ARN")

_KNOWN_STAGES = [
    "upload-ingestion",
    "pdf-parsing-chunking",
    "classification-template-matching",
    "metadata-summary-extraction",
    "embedding-indexing",
    "reranker",
    "module26-narrative-writers",
    "module26-tabulators",
    "module24-synthesizer",
    "validation-scoring",
    "packaging-submission",
    "pdf-extraction",
    "zeroshot-labeling",
]


def handler(event: Dict[str, Any], _context: Any | None = None) -> Dict[str, Any]:
    """Process S3 and SNS events to keep document pipeline status in sync."""
    logger.debug("Received event: %s", json.dumps(event))
    if "Records" not in event:
        logger.info("Event has no Records; returning no-op response.")
        return {"status": "ignored"}

    responses: List[Dict[str, Any]] = []
    for record in event["Records"]:
        event_source = record.get("eventSource") or record.get("EventSource")
        if event_source == "aws:s3":
            response = _handle_s3_record(record)
        elif event_source == "aws:sns":
            response = _handle_sns_record(record)
        else:
            logger.debug("Ignoring record with eventSource=%s", event_source)
            response = {
                "status": "ignored",
                "reason": f"unsupported event source {event_source}",
            }
        responses.append(response)

    return {"status": "ok", "responses": responses}


def _get_dynamodb_resource() -> Any:
    global dynamodb
    if dynamodb is None:
        if boto3 is None:
            raise RuntimeError("boto3 is required to access DynamoDB.")
        dynamodb = boto3.resource("dynamodb")
    return dynamodb


def _get_sns_client() -> Any:
    global sns
    if sns is None:
        if boto3 is None:
            raise RuntimeError("boto3 is required to publish SNS messages.")
        sns = boto3.client("sns")
    return sns


def _handle_s3_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Persist ingest metadata for a new S3 object and send a completion notice."""
    s3_info = record.get("s3", {})
    bucket = s3_info.get("bucket", {}).get("name")
    object_info = s3_info.get("object", {})
    key = object_info.get("key")
    version_id = object_info.get("versionId")

    if not bucket or not key:
        return {"status": "ignored", "reason": "missing bucket or key"}

    if not _is_allowed_extension(key):
        logger.info(
            "Skipping object with unsupported extension: s3://%s/%s", bucket, key
        )
        return {"status": "ignored", "reason": "unsupported extension"}

    document_id = _build_document_id(bucket, key, version_id)
    uploaded_ts = _extract_event_time(record)

    table = _get_table()
    table.put_item(
        Item={
            "document_id": document_id,
            "s3_bucket": bucket,
            "s3_key": key,
            "s3_version_id": version_id or "",
            "company": _DEFAULT_COMPANY,
            "project": _DEFAULT_PROJECT,
            "status": "ingest.completed",
            "stage_status": {
                "upload-ingestion": "completed",
            },
            "created_at": uploaded_ts,
            "updated_at": uploaded_ts,
        }
    )

    logger.info("Recorded document %s in DynamoDB", document_id)

    payload = {
        "company": _DEFAULT_COMPANY,
        "project": _DEFAULT_PROJECT,
        "file": key.split("/")[-1],
        "metadata": {
            "s3_uri": f"s3://{bucket}/{key}",
            "s3_bucket": bucket,
            "s3_key": key,
            "s3_version_id": version_id,
            "document_id": document_id,
        },
    }

    if not _COMPLETION_TOPIC_ARN:
        logger.warning("INGEST_COMPLETED_TOPIC_ARN not set; skipping SNS publish.")
    else:
        _get_sns_client().publish(
            TopicArn=_COMPLETION_TOPIC_ARN,
            Message=json.dumps(payload),
        )
        logger.info("Published ingest.completed message for %s", document_id)

    return {"status": "processed", "document_id": document_id}


def _handle_sns_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Update the pipeline stage state in DynamoDB based on SNS callbacks."""
    sns_payload = record.get("Sns", {})
    message_str = sns_payload.get("Message", "")
    try:
        message = json.loads(message_str)
    except json.JSONDecodeError:
        logger.warning("Received non-JSON SNS message: %s", message_str)
        return {"status": "ignored", "reason": "non-json message"}

    stage = message.get("stage") or message.get("module")
    status = message.get("status") or message.get("stage_status") or "completed"

    document_id, attrs = _extract_document_attributes(message)
    if not document_id:
        logger.warning(
            "SNS message missing document identifier; skipping update: %s", message
        )
        return {"status": "ignored", "reason": "missing document id"}

    table = _get_table()
    now_iso = _utc_now()

    update_expression = "SET updated_at = :updated"
    expression_values: Dict[str, Any] = {":updated": now_iso}
    if stage:
        stage_key = stage if stage in _KNOWN_STAGES else str(stage)
        update_expression += ", stage_status.#stage = :stage_status"
        expression_values[":stage_status"] = status
        expression_names = {"#stage": stage_key}
    else:
        expression_names = None

    if message.get("output"):
        update_expression += ", last_output = :output"
        expression_values[":output"] = message["output"]
    if message.get("input"):
        update_expression += ", last_input = :input"
        expression_values[":input"] = message["input"]

    table.update_item(
        Key={"document_id": document_id},
        UpdateExpression=update_expression,
        ExpressionAttributeValues=expression_values,
        ExpressionAttributeNames=expression_names,
    )

    logger.info(
        "Updated document %s for stage %s with status %s", document_id, stage, status
    )

    return {
        "status": "updated",
        "document_id": document_id,
        "stage": stage,
        "stage_status": status,
        **(attrs or {}),
    }


def _get_table():
    """Return the configured DynamoDB table resource."""
    if not _TABLE_NAME:
        raise RuntimeError("PIPELINE_STATUS_TABLE environment variable is required")
    return _get_dynamodb_resource().Table(_TABLE_NAME)


def _is_allowed_extension(key: str) -> bool:
    """Return True when the uploaded key matches an allowed file extension."""
    _, _, suffix = key.rpartition(".")
    return suffix.lower() in _ALLOWED_EXTENSIONS


def _build_document_id(bucket: str, key: str, version_id: Optional[str]) -> str:
    """Create a stable document identifier from bucket/key/version."""
    base = f"{bucket}/{key}"
    if version_id:
        return f"{base}#{version_id}"
    return base


def _extract_document_attributes(
    message: Dict[str, Any],
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Extract document identifiers and metadata from a pipeline message."""
    attrs: Dict[str, Any] = {}
    doc_id = None

    metadata = {}
    if isinstance(message.get("metadata"), dict):
        metadata = message["metadata"]
    elif isinstance(message.get("output"), dict):
        metadata = message["output"].get("metadata", {})

    if isinstance(metadata, dict):
        doc_id = metadata.get("document_id")
        if not doc_id:
            s3_uri = metadata.get("s3_uri")
            if s3_uri:
                doc_id = s3_uri.replace("s3://", "")
        attrs["metadata"] = metadata

    if isinstance(message.get("input"), dict):
        attrs["input"] = message["input"]
        input_meta = (
            message["input"].get("metadata")
            if isinstance(message["input"], dict)
            else None
        )
        if isinstance(input_meta, dict) and not doc_id:
            doc_id = input_meta.get("document_id")

    if isinstance(message.get("output"), dict):
        output = message["output"]
        if "analysis_s3_uri" in output and not doc_id:
            doc_id = output["analysis_s3_uri"].replace("s3://", "")
        attrs["output"] = output

    if not doc_id and "document_id" in message:
        doc_id = message["document_id"]

    return doc_id, attrs
