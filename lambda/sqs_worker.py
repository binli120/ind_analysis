"""
SQS worker: downloads PDF from S3, writes documents/document_versions,
extracts content, persists to DB, uploads markdown/quality artifacts,
and runs LangChain extraction (optional).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

import boto3

from pdf_analysis.pipeline.pipeline import PDFProcessingPipeline, PipelineContext
from pdf_analysis.pipeline.langchain_extraction import (
    DocumentChunk,
    LangChainExtractionPipeline,
)
from pdf_analysis.pipeline.langchain_chains import (
    build_noael_chain,
    build_pk_chain,
    build_study_segmentation_chain,
)
from ncd.db_interface import NCDRepository

s3 = boto3.client("s3")

ENABLE_LANGCHAIN = os.getenv("ENABLE_LANGCHAIN", "true").lower() == "true"
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
DEFAULT_TENANT_ID = os.getenv("DEFAULT_TENANT_ID")
DEFAULT_USER_ID = os.getenv("DEFAULT_USER_ID")


def handler(event: Dict[str, Any], _ctx=None) -> Dict[str, Any]:
    responses: List[Dict[str, Any]] = []
    for record in event.get("Records", []):
        try:
            body = record.get("body") or "{}"
            payload = json.loads(body)
            resp = process_message(payload)
            responses.append({"status": "ok", "message_id": record.get("messageId"), "result": resp})
        except Exception as exc:  # pragma: no cover - lambda runtime logs
            responses.append(
                {"status": "error", "message_id": record.get("messageId"), "error": str(exc)}
            )
    return {"status": "ok", "responses": responses}


def process_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    bucket = payload["bucket"]
    key = payload["key"]
    version_id = payload.get("version_id")
    tenant_id = payload.get("tenant_id") or DEFAULT_TENANT_ID
    created_by = payload.get("created_by") or DEFAULT_USER_ID
    if not tenant_id:
        raise RuntimeError("tenant_id is required (set DEFAULT_TENANT_ID or include in payload)")

    with tempfile.TemporaryDirectory() as tmpdir:
        local_pdf = Path(tmpdir) / "input.pdf"
        download_kwargs = {"Bucket": bucket, "Key": key}
        if version_id:
            download_kwargs["VersionId"] = version_id
        s3.download_file(**download_kwargs, Filename=str(local_pdf))

        content_hash = sha256_file(local_pdf)

        repo = NCDRepository()
        existing_status = repo.fetch_ingestion_status(
            s3_bucket=bucket, s3_key=key, content_hash=content_hash
        )
        if existing_status and existing_status.get("status") == "completed":
            return {
                "status": "skipped_existing",
                "document_version_id": existing_status.get("document_version_id"),
            }
        repo.upsert_ingestion_status(
            s3_bucket=bucket,
            s3_key=key,
            s3_version_id=version_id,
            content_hash=content_hash,
            status="processing",
        )

        pdf_pipeline = PDFProcessingPipeline()
        result = pdf_pipeline.run(local_pdf)
        page_count = len(result.pages)
        ctx = PipelineContext(pdf_path=local_pdf, pages=result.pages, tables=result.tables)
        chunks = pdf_pipeline._build_document_chunks(ctx)

        document_id, document_version_id = repo.ensure_document_and_version(
            tenant_id=tenant_id,
            title=Path(key).name,
            s3_bucket=bucket,
            s3_key=key,
            s3_version_id=version_id,
            file_type=_infer_file_type(key),
            content_hash=content_hash,
            page_count=page_count,
            created_by=created_by,
        )

        markdown_key = f"{key}.extracted.md"
        quality_key = f"{key}.quality.json"
        if result.markdown:
            s3.put_object(
                Bucket=bucket,
                Key=markdown_key,
                Body=result.markdown.encode("utf-8"),
                ContentType="text/markdown",
            )
        if result.quality_report:
            s3.put_object(
                Bucket=bucket,
                Key=quality_key,
                Body=json.dumps(result.quality_report).encode("utf-8"),
                ContentType="application/json",
            )

        lc_result: Dict[str, Any] | None = None
        if ENABLE_LANGCHAIN:
            _require_openai_api_key()
            seg_chain = build_study_segmentation_chain(model=LLM_MODEL)
            noael_chain = build_noael_chain(model=LLM_MODEL)
            pk_chain = build_pk_chain(model=LLM_MODEL)
            lc = LangChainExtractionPipeline(
                study_segmentation_chain=seg_chain,
                noael_chain=noael_chain,
                pk_chain=pk_chain,
                repository=repo,
            )
            lc_result = lc.run(
                document_version_id=document_version_id,
                source_document_id=document_id,
                extractor_name="langchain_lambda_v1",
                model_name=LLM_MODEL,
                chunks=chunks,
                created_by=created_by,
            )

        repo.upsert_ingestion_status(
            s3_bucket=bucket,
            s3_key=key,
            s3_version_id=version_id,
            content_hash=content_hash,
            status="completed",
            document_version_id=document_version_id,
        )

        return {
            "document_id": document_id,
            "document_version_id": document_version_id,
            "page_count": page_count,
            "markdown_key": markdown_key if result.markdown else None,
            "quality_key": quality_key if result.quality_report else None,
            "langchain": lc_result,
            "status": "completed",
        }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _infer_file_type(key: str) -> str:
    ext = key.lower().rsplit(".", 1)[-1]
    return ext if ext in {"pdf", "docx", "txt", "html"} else "pdf"


def _require_openai_api_key() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required when ENABLE_LANGCHAIN=true")
