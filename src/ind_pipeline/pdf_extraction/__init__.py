# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Pipeline stage that downloads PDFs from S3, runs extraction, and publishes results."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
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
from ind_pipeline.utils import build_analysis_key, parse_s3_uri, utc_timestamp
from pdf_analysis.pipeline import PDFProcessingPipeline

logger = logging.getLogger(__name__)

MODULE_NAME = "pdf-extraction"

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
    """Read an environment variable, raising if it is missing and no default is provided."""
    value = os.getenv(name, default)
    if value is None or not value.strip():
        raise RuntimeError(
            f"Environment variable {name} is required for the {MODULE_NAME} module"
        )
    return value


def _extract_context(payload: Dict[str, Any]) -> Tuple[str, str, str, Dict[str, Any]]:
    """Validate and extract company/project/file metadata from the SNS payload."""
    company = str(payload.get("company") or "").strip()
    project = str(payload.get("project") or "").strip()
    file_name = str(payload.get("file") or "").strip()
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata field must be an object")

    if not company:
        raise ValueError("payload missing 'company'")
    if not project:
        raise ValueError("payload missing 'project'")
    if not file_name:
        raise ValueError("payload missing 'file'")

    return company, project, file_name, metadata


def _download_pdf(bucket: str, key: str, version_id: Optional[str]) -> Path:
    """Download the PDF to a temporary path, respecting optional version IDs."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp_path = Path(tmp.name)
    extra_args = {"VersionId": version_id} if version_id else None
    try:
        s3_client = _get_s3_client()
        if extra_args:
            s3_client.download_file(bucket, key, str(tmp_path), ExtraArgs=extra_args)
        else:
            s3_client.download_file(bucket, key, str(tmp_path))
    except ClientError as exc:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to download s3://{bucket}/{key}: {exc}") from exc
    return tmp_path


def _store_analysis(bucket: str, key: str, document: Dict[str, Any]) -> str:
    """Persist the analysis JSON next to the source document and return its key."""
    analysis_key = build_analysis_key(key, "analysis.json")
    try:
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=analysis_key,
            Body=json.dumps(document).encode("utf-8"),
            ContentType="application/json",
        )
    except ClientError as exc:
        raise RuntimeError(
            f"Failed to upload analysis to s3://{bucket}/{analysis_key}: {exc}"
        ) from exc
    return analysis_key


def _run_pipeline(temp_pdf: Path) -> Any:
    """Execute the PDFProcessingPipeline on the downloaded file."""
    pipeline = PDFProcessingPipeline()
    return pipeline.run(temp_pdf)


def _iter_next_topic_arns() -> List[str]:
    """Return downstream topic ARNs configured for this module."""
    topics: List[str] = []
    raw_list = os.getenv("PDF_EXTRACT_NEXT_TOPIC_ARNS")
    if raw_list:
        topics.extend(item.strip() for item in raw_list.split(",") if item.strip())
    single_topic = os.getenv("PDF_EXTRACT_NEXT_TOPIC_ARN")
    if single_topic:
        topics.append(single_topic.strip())
    seen: set[str] = set()
    unique: List[str] = []
    for topic in topics:
        if topic and topic not in seen:
            seen.add(topic)
            unique.append(topic)
    return unique


def _publish_next_events(
    request_payload: Dict[str, Any], result_payload: Dict[str, Any]
) -> None:
    """Broadcast completion events so the next stages can begin processing."""
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
            logger.warning(
                "[%s] failed to publish downstream event to %s: %s",
                MODULE_NAME,
                topic,
                exc,
            )


def pdf_extraction_handler(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Entry point invoked by NotificationConsumer for each document payload."""
    company, project, file_name, metadata = _extract_context(payload)
    s3_uri = metadata.get("s3_uri")
    if not s3_uri:
        raise ValueError("metadata.s3_uri is required")
    bucket, key = parse_s3_uri(str(s3_uri))
    version_id = metadata.get("s3_version_id")

    temp_pdf = _download_pdf(bucket, key, version_id)
    try:
        pipeline_result = _run_pipeline(temp_pdf)
    finally:
        temp_pdf.unlink(missing_ok=True)

    metrics_payload: Optional[Dict[str, Any]] = None
    if pipeline_result.metrics:
        metrics_payload = asdict(pipeline_result.metrics)

    analysis_document: Dict[str, Any] = {
        "company": company,
        "project": project,
        "file": file_name,
        "source": {
            "bucket": bucket,
            "key": key,
            "version_id": version_id,
        },
        "metadata": metadata,
        "generated_at": utc_timestamp(),
        "analysis": {
            "text_engine": pipeline_result.text_engine,
            "ocr_strategy": pipeline_result.ocr_strategy,
            "table_engines": list(pipeline_result.table_engines),
            "metrics": metrics_payload,
        },
        "markdown": pipeline_result.markdown,
        "html": pipeline_result.html,
        "quality_report": pipeline_result.quality_report,
        "quality_markdown": pipeline_result.quality_markdown,
        "key_values": pipeline_result.key_values,
    }

    analysis_key = _store_analysis(bucket, key, analysis_document)
    analysis_s3_uri = f"s3://{bucket}/{analysis_key}"

    logger.info(
        "[%s] processed %s/%s -> %s",
        MODULE_NAME,
        bucket,
        key,
        analysis_s3_uri,
    )

    result_payload = {
        "company": company,
        "project": project,
        "file": file_name,
        "analysis_s3_uri": analysis_s3_uri,
        "text_engine": pipeline_result.text_engine,
        "ocr_strategy": pipeline_result.ocr_strategy,
        "table_engines": list(pipeline_result.table_engines),
        "metrics": metrics_payload,
        "stage": MODULE_NAME,
        "status": "completed",
    }

    _publish_next_events(payload, result_payload)
    return result_payload


def run() -> None:
    """Start the NotificationConsumer loop for the pdf-extraction module."""
    topic_arn = _resolve_env("PDF_EXTRACT_TOPIC_ARN", os.getenv("SNS_TOPIC_ARN"))
    queue_name = _resolve_env("PDF_EXTRACT_QUEUE_NAME", "pdf-extraction-queue")
    completion_topic_arn = os.getenv("PDF_EXTRACT_COMPLETION_TOPIC_ARN")
    wait_time = int(os.getenv("PDF_EXTRACT_WAIT_TIME_SECONDS", "20"))
    poll_interval = int(os.getenv("PDF_EXTRACT_POLL_INTERVAL", "5"))
    max_messages = int(os.getenv("PDF_EXTRACT_MAX_MESSAGES", "10"))

    config = ModuleConfig(
        name=MODULE_NAME,
        topic_arn=topic_arn,
        queue_name=queue_name,
        completion_topic_arn=completion_topic_arn,
        wait_time_seconds=wait_time,
        poll_interval=poll_interval,
        max_messages=max_messages,
    )

    consumer = NotificationConsumer(config, pdf_extraction_handler)
    consumer.run()


register_module(
    ModuleDescriptor(
        name=MODULE_NAME,
        description="Extract text, tables, and metadata from PDFs stored in S3.",
        entrypoint=run,
    )
)
