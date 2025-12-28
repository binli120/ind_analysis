# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Zero-shot labeling stage that enriches extracted markdown with OpenAI metadata."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

try:
    import boto3
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    boto3 = None  # type: ignore[assignment]
try:
    from botocore.exceptions import ClientError
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    ClientError = Exception  # type: ignore[assignment]

from ind_pipeline.consumer import ModuleConfig, NotificationConsumer
from ind_pipeline.registry import ModuleDescriptor, register_module
from ind_pipeline.utils import build_metadata_key, parse_s3_uri, utc_timestamp
from pdf_analysis.service.ai_metadata import OpenAIMetadataGenerator

logger = logging.getLogger(__name__)

MODULE_NAME = "zeroshot-labeling"

_generator: Optional[OpenAIMetadataGenerator] = None
_s3_client: Any | None = None
_sns_client: Any | None = None


def _get_s3_client() -> Any:
    global _s3_client
    if _s3_client is None:
        if boto3 is None:
            raise RuntimeError("boto3 is required to access S3.")
        _s3_client = boto3.client("s3")
    return _s3_client


def _get_sns_client() -> Any:
    global _sns_client
    if _sns_client is None:
        if boto3 is None:
            raise RuntimeError("boto3 is required to publish SNS messages.")
        _sns_client = boto3.client("sns")
    return _sns_client


def _resolve_env(name: str, default: Optional[str] = None) -> str:
    """Read an environment variable, raising when missing and no default is set."""
    value = os.getenv(name, default)
    if value is None or not value.strip():
        raise RuntimeError(f"Environment variable {name} is required for the {MODULE_NAME} module")
    return value


def _get_generator() -> OpenAIMetadataGenerator:
    """Instantiate (and cache) the OpenAI metadata generator."""
    global _generator
    if _generator is None:
        model = os.getenv("ZEROSHOT_OPENAI_MODEL", "gpt-4o-mini")
        _generator = OpenAIMetadataGenerator(model=model)
    if getattr(_generator, "_client", None) is None:
        raise RuntimeError(
            "OpenAI client is not configured. Ensure OPENAI_API_KEY is set for zero-shot labeling."
        )
    return _generator


def _load_analysis_document(analysis_s3_uri: str) -> Tuple[Dict[str, Any], str, str]:
    """Fetch and parse the analysis JSON produced by the extraction module."""
    bucket, key = parse_s3_uri(analysis_s3_uri)
    try:
        response = _get_s3_client().get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        raise RuntimeError(f"Failed to download analysis document {analysis_s3_uri}: {exc}") from exc
    body = response["Body"].read()
    try:
        document = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Analysis document {analysis_s3_uri} is not valid JSON") from exc
    return document, bucket, key


def _store_metadata(bucket: str, object_key: str, document: Dict[str, Any]) -> str:
    """Write the classification metadata JSON back to S3."""
    metadata_key = build_metadata_key(object_key, "classification.json")
    try:
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=metadata_key,
            Body=json.dumps(document).encode("utf-8"),
            ContentType="application/json",
        )
    except ClientError as exc:
        raise RuntimeError(f"Failed to upload classification to s3://{bucket}/{metadata_key}: {exc}") from exc
    return metadata_key


def _iter_next_topic_arns() -> List[str]:
    """Return downstream topic ARNs configured for this module."""
    topics: List[str] = []
    raw_list = os.getenv("ZEROSHOT_NEXT_TOPIC_ARNS")
    if raw_list:
        topics.extend(item.strip() for item in raw_list.split(",") if item.strip())
    single_topic = os.getenv("ZEROSHOT_NEXT_TOPIC_ARN")
    if single_topic:
        topics.append(single_topic.strip())
    seen: set[str] = set()
    unique: List[str] = []
    for topic in topics:
        if topic and topic not in seen:
            seen.add(topic)
            unique.append(topic)
    return unique


def _publish_next_events(request_payload: Dict[str, Any], result_payload: Dict[str, Any]) -> None:
    """Send completion events to all configured downstream topics."""
    topics = _iter_next_topic_arns()
    if not topics:
        return

    message_payload = {
        "stage": MODULE_NAME,
        "status": result_payload.get("status", "completed"),
        "completed_at": utc_timestamp(),
        "input": request_payload,
        "output": result_payload,
    }
    message = json.dumps(message_payload)

    for topic in topics:
        try:
            _get_sns_client().publish(TopicArn=topic, Message=message)
            logger.info("[%s] published downstream event to %s", MODULE_NAME, topic)
        except ClientError as exc:  # pragma: no cover - network failure
            logger.warning("[%s] failed to publish downstream event to %s: %s", MODULE_NAME, topic, exc)


def zeroshot_handler(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Process a notification payload and emit zero-shot classification metadata."""
    analysis_document: Dict[str, Any] = {}
    analysis_bucket: Optional[str] = None
    analysis_key: Optional[str] = None

    analysis_s3_uri = payload.get("analysis_s3_uri")
    if analysis_s3_uri:
        analysis_document, analysis_bucket, analysis_key = _load_analysis_document(str(analysis_s3_uri))

    company = (payload.get("company") or analysis_document.get("company") or "").strip()
    project = (payload.get("project") or analysis_document.get("project") or "").strip()
    file_name = (payload.get("file") or analysis_document.get("file") or "").strip()

    if not company:
        raise ValueError("company is required for zero-shot labeling")
    if not project:
        raise ValueError("project is required for zero-shot labeling")
    if not file_name:
        raise ValueError("file is required for zero-shot labeling")

    source_info = analysis_document.get("source", {})
    source_bucket = source_info.get("bucket") or analysis_bucket
    source_key = source_info.get("key") or analysis_key
    if not source_bucket or not source_key:
        raise ValueError("Unable to determine source S3 object for classification results")

    markdown = payload.get("markdown") or analysis_document.get("markdown")
    if not markdown:
        raise ValueError("No markdown content available for zero-shot labeling")

    generator = _get_generator()
    metadata = generator(str(markdown))
    if not metadata:
        raise RuntimeError("Zero-shot labeling returned no metadata")

    classification_document = {
        "company": company,
        "project": project,
        "file": file_name,
        "source": {
            "bucket": source_bucket,
            "key": source_key,
        },
        "analysis": {
            "bucket": analysis_bucket,
            "key": analysis_key,
            "uri": analysis_s3_uri,
        },
        "generated_at": utc_timestamp(),
        "metadata": metadata,
    }

    metadata_key = _store_metadata(source_bucket, source_key, classification_document)
    metadata_s3_uri = f"s3://{source_bucket}/{metadata_key}"

    logger.info(
        "[%s] classified %s/%s -> %s",
        MODULE_NAME,
        source_bucket,
        source_key,
        metadata_s3_uri,
    )

    result_payload = {
        "company": company,
        "project": project,
        "file": file_name,
        "metadata_s3_uri": metadata_s3_uri,
        "labels": metadata.get("labels", []),
        "keywords": metadata.get("keywords", []),
        "language": metadata.get("language"),
        "ind_document_type": metadata.get("ind_document_type"),
        "ind_section_number": metadata.get("ind_section_number"),
        "ind_section_title": metadata.get("ind_section_title"),
        "ind_classification_confidence": metadata.get("ind_classification_confidence"),
        "stage": MODULE_NAME,
        "status": "completed",
    }

    _publish_next_events(payload, result_payload)
    return result_payload


def run() -> None:
    """Start the NotificationConsumer loop for zero-shot labeling."""
    topic_arn = _resolve_env("ZEROSHOT_TOPIC_ARN", os.getenv("SNS_TOPIC_ARN"))
    queue_name = _resolve_env("ZEROSHOT_QUEUE_NAME", "zeroshot-labeling-queue")
    completion_topic_arn = os.getenv("ZEROSHOT_COMPLETION_TOPIC_ARN")
    wait_time = int(os.getenv("ZEROSHOT_WAIT_TIME_SECONDS", "20"))
    poll_interval = int(os.getenv("ZEROSHOT_POLL_INTERVAL", "5"))
    max_messages = int(os.getenv("ZEROSHOT_MAX_MESSAGES", "10"))

    config = ModuleConfig(
        name=MODULE_NAME,
        topic_arn=topic_arn,
        queue_name=queue_name,
        completion_topic_arn=completion_topic_arn,
        wait_time_seconds=wait_time,
        poll_interval=poll_interval,
        max_messages=max_messages,
    )

    consumer = NotificationConsumer(config, zeroshot_handler)
    consumer.run()


register_module(
    ModuleDescriptor(
        name=MODULE_NAME,
        description="Perform zero-shot classification and labeling on extracted markdown.",
        entrypoint=run,
    )
)
