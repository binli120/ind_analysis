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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import boto3

from pdf_analysis.pipeline.pipeline import PDFProcessingPipeline, PipelineContext
from pdf_analysis.pipeline.config import PipelineConfig
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


def process_message(
    payload: Dict[str, Any],
    *,
    force: bool = False,
    run_core: bool = True,
    run_langchain: bool = True,
) -> Dict[str, Any]:
    bucket = payload["bucket"]
    key = payload["key"]
    version_id = payload.get("version_id")
    tenant_id = payload.get("tenant_id") or DEFAULT_TENANT_ID
    created_by = payload.get("created_by") or DEFAULT_USER_ID
    if not tenant_id:
        raise RuntimeError("tenant_id is required (set DEFAULT_TENANT_ID or include in payload)")

    content_hash: Optional[str] = None
    document_version_id: Optional[str] = None
    document_id: Optional[str] = None
    core_status = "skipped"
    langchain_status = "skipped"
    core_error: Optional[str] = None
    langchain_error: Optional[str] = None
    core_duration = 0.0
    langchain_duration = 0.0
    repo = NCDRepository()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            local_pdf = Path(tmpdir) / "input.pdf"
            download_kwargs = {"Bucket": bucket, "Key": key}
            extra_args = {"VersionId": version_id} if version_id else None
            s3.download_file(**download_kwargs, Filename=str(local_pdf), ExtraArgs=extra_args)

            content_hash = sha256_file(local_pdf)

            pipeline_result = None
            chunks = None
            page_count = None

            try:
                core_status_row = repo.fetch_pipeline_status(
                    s3_bucket=bucket,
                    s3_key=key,
                    content_hash=content_hash,
                    pipeline="core",
                )
            except Exception:
                core_status_row = None
            if core_status_row and core_status_row.get("document_version_id"):
                document_version_id = str(core_status_row.get("document_version_id"))

            core_skip = False
            if run_core and not force and core_status_row and core_status_row.get("status") == "completed":
                core_skip = True

            if run_core and not core_skip:
                started = datetime.now(timezone.utc)
                repo.upsert_pipeline_status(
                    s3_bucket=bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    content_hash=content_hash,
                    pipeline="core",
                    status="processing",
                )
                repo.upsert_ingestion_status(
                    s3_bucket=bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    content_hash=content_hash,
                    status="processing",
                )

                pdf_pipeline = _build_pipeline()
                pipeline_result = pdf_pipeline.run(local_pdf)
                page_count = len(pipeline_result.pages)
                ctx = PipelineContext(
                    pdf_path=local_pdf,
                    pages=pipeline_result.pages,
                    tables=pipeline_result.tables,
                )
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
                if pipeline_result.markdown:
                    s3.put_object(
                        Bucket=bucket,
                        Key=markdown_key,
                        Body=pipeline_result.markdown.encode("utf-8"),
                        ContentType="text/markdown",
                    )
                if pipeline_result.quality_report:
                    s3.put_object(
                        Bucket=bucket,
                        Key=quality_key,
                        Body=json.dumps(pipeline_result.quality_report).encode("utf-8"),
                        ContentType="application/json",
                    )

                repo.upsert_pipeline_status(
                    s3_bucket=bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    content_hash=content_hash,
                    pipeline="core",
                    status="completed",
                    document_version_id=document_version_id,
                )
                repo.upsert_ingestion_status(
                    s3_bucket=bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    content_hash=content_hash,
                    status="completed",
                    document_version_id=document_version_id,
                )
                core_status = "completed"
                core_duration = (datetime.now(timezone.utc) - started).total_seconds()
            elif core_skip:
                core_status = "skipped_existing"

            if not document_version_id and content_hash:
                existing_status = repo.fetch_ingestion_status(
                    s3_bucket=bucket, s3_key=key, content_hash=content_hash
                )
                if existing_status and existing_status.get("document_version_id"):
                    document_version_id = str(existing_status.get("document_version_id"))
            if document_version_id and not document_id:
                document_id = repo.fetch_document_id_for_version(document_version_id)

            lc_result: Dict[str, Any] | None = None
            if run_langchain:
                try:
                    if not ENABLE_LANGCHAIN:
                        langchain_status = "skipped"
                        repo.upsert_pipeline_status(
                            s3_bucket=bucket,
                            s3_key=key,
                            s3_version_id=version_id,
                            content_hash=content_hash,
                            pipeline="langchain",
                            status="skipped",
                            error_message="ENABLE_LANGCHAIN=false",
                        )
                    else:
                        if not document_version_id:
                            raise RuntimeError("document_version_id is required for langchain pipeline")
                        if not document_id:
                            raise RuntimeError("document_id is required for langchain pipeline")
                        started = datetime.now(timezone.utc)
                        repo.upsert_pipeline_status(
                            s3_bucket=bucket,
                            s3_key=key,
                            s3_version_id=version_id,
                            content_hash=content_hash,
                            pipeline="langchain",
                            status="processing",
                            document_version_id=document_version_id,
                        )
                        _require_openai_api_key()
                        if chunks is None:
                            pdf_pipeline = _build_pipeline()
                            pipeline_result = pdf_pipeline.run(local_pdf)
                            ctx = PipelineContext(
                                pdf_path=local_pdf,
                                pages=pipeline_result.pages,
                                tables=pipeline_result.tables,
                            )
                            chunks = pdf_pipeline._build_document_chunks(ctx)
                            page_count = len(pipeline_result.pages)

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
                            chunks=chunks or [],
                            created_by=created_by,
                        )
                        repo.upsert_pipeline_status(
                            s3_bucket=bucket,
                            s3_key=key,
                            s3_version_id=version_id,
                            content_hash=content_hash,
                            pipeline="langchain",
                            status="completed",
                            document_version_id=document_version_id,
                        )
                        langchain_status = "completed"
                        langchain_duration = (datetime.now(timezone.utc) - started).total_seconds()
                except Exception as exc:
                    langchain_error = _format_error(exc)
                    langchain_status = "failed"
                    repo.upsert_pipeline_status(
                        s3_bucket=bucket,
                        s3_key=key,
                        s3_version_id=version_id,
                        content_hash=content_hash,
                        pipeline="langchain",
                        status="failed",
                        document_version_id=document_version_id,
                        error_message=langchain_error,
                    )

            return {
                "document_id": document_id,
                "document_version_id": document_version_id,
                "page_count": page_count,
                "core_status": core_status,
                "core_error": core_error,
                "core_duration_seconds": round(core_duration, 3),
                "langchain_status": langchain_status,
                "langchain_error": langchain_error,
                "langchain_duration_seconds": round(langchain_duration, 3),
                "langchain": lc_result,
                "status": "completed" if core_status == "completed" else core_status,
            }
    except Exception as exc:
        try:
            repo.session.rollback()
        except Exception:
            pass
        err_msg = _format_error(exc)
        if content_hash and run_core:
            repo.upsert_pipeline_status(
                s3_bucket=bucket,
                s3_key=key,
                s3_version_id=version_id,
                content_hash=content_hash,
                pipeline="core",
                status="failed",
                document_version_id=document_version_id,
                error_message=err_msg,
            )
            repo.upsert_ingestion_status(
                s3_bucket=bucket,
                s3_key=key,
                s3_version_id=version_id,
                content_hash=content_hash,
                status="failed",
                document_version_id=document_version_id,
                error_message=err_msg,
            )
        if content_hash and run_langchain and langchain_status == "processing":
            repo.upsert_pipeline_status(
                s3_bucket=bucket,
                s3_key=key,
                s3_version_id=version_id,
                content_hash=content_hash,
                pipeline="langchain",
                status="failed",
                document_version_id=document_version_id,
                error_message=err_msg,
            )
        raise


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


def _format_error(exc: Exception, limit: int = 2000) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if len(message) > limit:
        return f"{message[:limit - 3]}..."
    return message


def _build_pipeline() -> PDFProcessingPipeline:
    """
    Build the PDF pipeline with optional overrides from env vars.
    PDF_PIPELINE_TABLE_ENGINES: comma-separated list (e.g., "pdfplumber,camelot").
    PDF_PIPELINE_DISABLE_TABLES: set to "1" to disable table extraction.
    PDF_PIPELINE_TEXT_ENGINES: comma-separated list (e.g., "pymupdf,pdfminer").
    """
    config = PipelineConfig()
    text_engines = os.getenv("PDF_PIPELINE_TEXT_ENGINES")
    if text_engines:
        engines = tuple(e.strip() for e in text_engines.split(",") if e.strip())
        if engines:
            config.text.engines = engines
    disable_tables = os.getenv("PDF_PIPELINE_DISABLE_TABLES", "").lower() in {"1", "true", "yes"}
    if disable_tables:
        config.structured.table_engines = ()
        return PDFProcessingPipeline(config=config)

    table_engines = os.getenv("PDF_PIPELINE_TABLE_ENGINES")
    if table_engines:
        engines = tuple(e.strip() for e in table_engines.split(",") if e.strip())
        if engines:
            config.structured.table_engines = engines
    return PDFProcessingPipeline(config=config)
