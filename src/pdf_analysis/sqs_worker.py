# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

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

import boto3
from sqlalchemy import text as sqltext

from pdf_analysis.export.persist import ensure_unique_columns, save_tables
from pdf_analysis.ingest.images import extract_images
from pdf_analysis.pipeline.pipeline import PDFProcessingPipeline, PipelineContext
from pdf_analysis.pipeline.config import PipelineConfig
from pdf_analysis.pipeline.langchain_extraction import (
    LangChainExtractionPipeline,
)
from pdf_analysis.pipeline.langchain_chains import (
    build_noael_chain,
    build_pk_chain,
    build_study_segmentation_chain,
)
from database.db_interface import (
    DocumentAssetRecord,
    DocumentKeySectionRecord,
    NCDRepository,
)
from ncd.extraction.content_extractor import (
    describe_image_asset,
    describe_table_asset,
    extract_key_sections_from_pages,
)
from ncd.extraction.fast_extract import match_fast_extract
from ncd.llm.llm_client import LLMClient

s3 = boto3.client("s3")

ENABLE_LANGCHAIN = os.getenv("ENABLE_LANGCHAIN", "true").lower() == "true"
ENABLE_CONTEXT_PIPELINE = os.getenv("ENABLE_CONTEXT_PIPELINE", "true").lower() == "true"
CONTEXT_DISABLE_IMAGES = os.getenv("CONTEXT_DISABLE_IMAGES", "").lower() in {
    "1",
    "true",
    "yes",
}
CONTEXT_DISABLE_ASSET_LLM = os.getenv("CONTEXT_DISABLE_ASSET_LLM", "").lower() in {
    "1",
    "true",
    "yes",
}
CONTEXT_DISABLE_KEY_SECTIONS = os.getenv(
    "CONTEXT_DISABLE_KEY_SECTIONS", ""
).lower() in {"1", "true", "yes"}
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
            responses.append(
                {"status": "ok", "message_id": record.get("messageId"), "result": resp}
            )
        except Exception as exc:  # pragma: no cover - lambda runtime logs
            responses.append(
                {
                    "status": "error",
                    "message_id": record.get("messageId"),
                    "error": str(exc),
                }
            )
    return {"status": "ok", "responses": responses}


def process_message(
    payload: Dict[str, Any],
    *,
    force: bool = False,
    run_core: bool = True,
    run_langchain: bool = True,
    run_context: bool = True,
) -> Dict[str, Any]:
    bucket = payload["bucket"]
    key = payload["key"]
    version_id = payload.get("version_id")
    tenant_id = payload.get("tenant_id") or DEFAULT_TENANT_ID
    project_id = payload.get("project_id") or os.getenv("PROJECT_ID")
    created_by = payload.get("created_by") or DEFAULT_USER_ID
    if not tenant_id:
        raise RuntimeError(
            "tenant_id is required (set DEFAULT_TENANT_ID or include in payload)"
        )

    content_hash: Optional[str] = None
    document_version_id: Optional[str] = None
    document_id: Optional[str] = None
    core_status = "skipped"
    langchain_status = "skipped"
    core_error: Optional[str] = None
    langchain_error: Optional[str] = None
    context_status = "skipped"
    context_error: Optional[str] = None
    core_duration = 0.0
    langchain_duration = 0.0
    context_duration = 0.0
    repo = NCDRepository()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            local_pdf = Path(tmpdir) / "input.pdf"
            download_kwargs = {"Bucket": bucket, "Key": key}
            extra_args = {"VersionId": version_id} if version_id else None
            s3.download_file(
                **download_kwargs, Filename=str(local_pdf), ExtraArgs=extra_args
            )

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
            if (
                run_core
                and not force
                and core_status_row
                and core_status_row.get("status") == "completed"
            ):
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
                    document_version_id = str(
                        existing_status.get("document_version_id")
                    )
            if document_version_id and not document_id:
                document_id = repo.fetch_document_id_for_version(document_version_id)

            lc_result: Dict[str, Any] | None = None
            if run_context and ENABLE_CONTEXT_PIPELINE:
                try:
                    try:
                        context_status_row = repo.fetch_pipeline_status(
                            s3_bucket=bucket,
                            s3_key=key,
                            content_hash=content_hash,
                            pipeline="context",
                        )
                    except Exception:
                        context_status_row = None
                    context_skip = False
                    if (
                        context_status_row
                        and context_status_row.get("status") == "completed"
                        and not force
                    ):
                        context_skip = True
                    if not context_skip:
                        if not document_version_id:
                            raise RuntimeError(
                                "document_version_id is required for context pipeline"
                            )
                        started = datetime.now(timezone.utc)
                        repo.upsert_pipeline_status(
                            s3_bucket=bucket,
                            s3_key=key,
                            s3_version_id=version_id,
                            content_hash=content_hash,
                            pipeline="context",
                            status="processing",
                            document_version_id=document_version_id,
                        )
                        _require_openai_api_key()
                        if pipeline_result is None:
                            pdf_pipeline = _build_pipeline()
                            pipeline_result = pdf_pipeline.run(local_pdf)

                        if force:
                            _purge_table_assets(
                                repo,
                                document_version_id,
                                f"{key}.tables/",
                            )
                            _delete_s3_prefix(bucket, f"{key}.tables/")

                        assets = _build_document_assets(
                            local_pdf,
                            pipeline_result.pages,
                            pipeline_result.tables,
                            bucket=bucket,
                            key=key,
                            llm=None if CONTEXT_DISABLE_ASSET_LLM else LLMClient(),
                        )
                        asset_rows = repo.upsert_document_assets(
                            [
                                DocumentAssetRecord(
                                    document_version_id=document_version_id, **asset
                                )
                                for asset in assets
                            ]
                        )
                        assets_by_page: Dict[int, List[str]] = {}
                        for row in asset_rows:
                            page_no = row.get("page_number")
                            if page_no is None:
                                continue
                            assets_by_page.setdefault(int(page_no), []).append(
                                str(row.get("id"))
                            )

                        if not CONTEXT_DISABLE_KEY_SECTIONS:
                            llm_client = LLMClient()
                            key_sections = extract_key_sections_from_pages(
                                llm_client, pipeline_result.pages
                            )
                            section_records: List[DocumentKeySectionRecord] = []
                            for section in key_sections:
                                page_start = section.get("page_start")
                                page_end = section.get("page_end")
                                asset_ids: List[str] = []
                                if page_start and page_end:
                                    for page_no in range(
                                        int(page_start), int(page_end) + 1
                                    ):
                                        asset_ids.extend(
                                            assets_by_page.get(page_no, [])
                                        )
                                section_records.append(
                                    DocumentKeySectionRecord(
                                        document_version_id=document_version_id,
                                        section_type=section.get("section_type"),
                                        text=section.get("text"),
                                        page_start=page_start,
                                        page_end=page_end,
                                        char_start=section.get("char_start"),
                                        char_end=section.get("char_end"),
                                        asset_ids=asset_ids,
                                        model_name=llm_client.model_name,
                                        confidence=section.get("confidence"),
                                    )
                                )
                            repo.replace_document_key_sections(section_records)
                        context_status = "completed"
                        context_duration = (
                            datetime.now(timezone.utc) - started
                        ).total_seconds()
                        repo.upsert_pipeline_status(
                            s3_bucket=bucket,
                            s3_key=key,
                            s3_version_id=version_id,
                            content_hash=content_hash,
                            pipeline="context",
                            status="completed",
                            document_version_id=document_version_id,
                        )
                    else:
                        context_status = "skipped_existing"
                except Exception as exc:
                    context_error = _format_error(exc)
                    context_status = "failed"
                    try:
                        repo.upsert_pipeline_status(
                            s3_bucket=bucket,
                            s3_key=key,
                            s3_version_id=version_id,
                            content_hash=content_hash,
                            pipeline="context",
                            status="failed",
                            document_version_id=document_version_id,
                            error_message=context_error,
                        )
                    except Exception:
                        pass
            elif run_context and not ENABLE_CONTEXT_PIPELINE:
                context_status = "skipped"
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
                            raise RuntimeError(
                                "document_version_id is required for langchain pipeline"
                            )
                        if not document_id:
                            raise RuntimeError(
                                "document_id is required for langchain pipeline"
                            )
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
                            project_id=str(project_id) if project_id else None,
                            source_key=key,
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
                        langchain_duration = (
                            datetime.now(timezone.utc) - started
                        ).total_seconds()
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
                "context_status": context_status,
                "context_error": context_error,
                "context_duration_seconds": round(context_duration, 3),
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
        if content_hash and run_context and context_status == "processing":
            repo.upsert_pipeline_status(
                s3_bucket=bucket,
                s3_key=key,
                s3_version_id=version_id,
                content_hash=content_hash,
                pipeline="context",
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
        raise RuntimeError(
            "OPENAI_API_KEY is required when ENABLE_LANGCHAIN or ENABLE_CONTEXT_PIPELINE is true"
        )


def _format_error(exc: Exception, limit: int = 2000) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if len(message) > limit:
        return f"{message[:limit - 3]}..."
    return message


def _build_document_assets(
    pdf_path: Path,
    pages: List[Dict[str, Any]],
    tables: List[Dict[str, Any]],
    *,
    bucket: str,
    key: str,
    llm: LLMClient | None,
) -> List[Dict[str, Any]]:
    assets: List[Dict[str, Any]] = []
    page_text: Dict[int, str] = {}
    for page in pages:
        try:
            page_no = int(page.get("page_number") or 0)
        except (TypeError, ValueError):
            continue
        page_text[page_no] = str(page.get("text") or "")

    with tempfile.TemporaryDirectory() as tmpdir:
        tables_dir = Path(tmpdir) / "tables"
        tables_dir.mkdir(parents=True, exist_ok=True)
        if tables:
            manifest = save_tables(pdf_path, tables, tables_dir)
            table_map = {
                (t.get("page_number"), t.get("index_on_page")): t for t in tables
            }
            for entry in manifest:
                page_number = entry.get("page_number")
                index_on_page = entry.get("index_on_page")
                csv_name = entry.get("csv")
                json_name = entry.get("json")
                if not csv_name or not json_name:
                    continue
                csv_path = tables_dir / csv_name
                json_path = tables_dir / json_name
                csv_key = f"{key}.tables/{csv_name}"
                json_key = f"{key}.tables/{json_name}"
                s3.upload_file(
                    str(csv_path),
                    bucket,
                    csv_key,
                    ExtraArgs={"ContentType": "text/csv"},
                )
                s3.upload_file(
                    str(json_path),
                    bucket,
                    json_key,
                    ExtraArgs={"ContentType": "application/json"},
                )
                df = None
                table = table_map.get((page_number, index_on_page))
                if table:
                    df = table.get("dataframe")
                preview_df = None
                if df is not None:
                    preview_df = ensure_unique_columns(df.fillna("").astype(str))
                description = None
                keywords: List[str] = []
                if llm and preview_df is not None:
                    try:
                        context = describe_table_asset(
                            llm,
                            page_number=page_number,
                            index_on_page=index_on_page,
                            columns=list(preview_df.columns),
                            preview_rows=preview_df.head(5).to_dict(orient="records"),
                            page_text=page_text.get(int(page_number or 0), ""),
                        )
                        description = context.get("description")
                        keywords = context.get("keywords") or []
                    except Exception:
                        description = None
                        keywords = []
                caption = f"Table p{page_number} t{index_on_page}"
                fast_matches = match_fast_extract(
                    page_text.get(int(page_number or 0), ""),
                    section_prefixes=("2.4", "2.6"),
                    extra_texts=[
                        caption,
                        (
                            " ".join(list(preview_df.columns))
                            if preview_df is not None
                            else ""
                        ),
                    ],
                )
                assets.append(
                    {
                        "asset_type": "table",
                        "page_number": page_number,
                        "index_on_page": index_on_page,
                        "s3_bucket": bucket,
                        "s3_key": csv_key,
                        "caption": caption,
                        "description": description,
                        "keywords": keywords,
                        "extra_attributes": {
                            "json_key": json_key,
                            "engine": table.get("engine") if table else None,
                            "columns": (
                                list(preview_df.columns)
                                if preview_df is not None
                                else []
                            ),
                            "row_count": (
                                int(preview_df.shape[0])
                                if preview_df is not None
                                else 0
                            ),
                            "fast_extract": fast_matches,
                        },
                    }
                )

        if not CONTEXT_DISABLE_IMAGES:
            images_dir = Path(tmpdir) / "images"
            images = extract_images(pdf_path, images_dir)
            for image in images:
                file_path = image.get("file_path")
                if not file_path:
                    continue
                page_number = image.get("page_number")
                index_on_page = image.get("index_on_page")
                caption = image.get("caption")
                file_name = Path(file_path).name
                image_key = f"{key}.images/{file_name}"
                s3.upload_file(
                    file_path,
                    bucket,
                    image_key,
                    ExtraArgs={"ContentType": "image/png"},
                )
                description = None
                keywords: List[str] = []
                if llm:
                    try:
                        context = describe_image_asset(
                            llm,
                            page_number=page_number,
                            index_on_page=index_on_page,
                            caption=caption,
                            page_text=page_text.get(int(page_number or 0), ""),
                        )
                        description = context.get("description")
                        keywords = context.get("keywords") or []
                    except Exception:
                        description = None
                        keywords = []
                fast_matches = match_fast_extract(
                    page_text.get(int(page_number or 0), ""),
                    section_prefixes=("2.4", "2.6"),
                    extra_texts=[caption or ""],
                )
                assets.append(
                    {
                        "asset_type": "image",
                        "page_number": page_number,
                        "index_on_page": index_on_page,
                        "s3_bucket": bucket,
                        "s3_key": image_key,
                        "caption": caption,
                        "description": description,
                        "keywords": keywords,
                        "extra_attributes": {
                            "bbox": image.get("bbox"),
                            "file_name": file_name,
                            "fast_extract": fast_matches,
                        },
                    }
                )
    return assets


def _delete_s3_prefix(bucket: str, prefix: str) -> None:
    paginator = s3.get_paginator("list_objects_v2")
    batch: List[Dict[str, str]] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key")
            if not key:
                continue
            batch.append({"Key": key})
            if len(batch) == 1000:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})
                batch = []
    if batch:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})


def _purge_table_assets(
    repo: NCDRepository, document_version_id: str, key_prefix: str
) -> None:
    repo.session.execute(
        sqltext(
            """
            DELETE FROM document_assets
            WHERE document_version_id = :dvid
              AND asset_type = 'table'
              AND s3_key LIKE :prefix
            """
        ),
        {"dvid": document_version_id, "prefix": f"{key_prefix}%"},
    )
    repo.session.commit()


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
    disable_tables = os.getenv("PDF_PIPELINE_DISABLE_TABLES", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if disable_tables:
        config.structured.table_engines = ()
        return PDFProcessingPipeline(config=config)

    table_engines = os.getenv("PDF_PIPELINE_TABLE_ENGINES")
    if table_engines:
        engines = tuple(e.strip() for e in table_engines.split(",") if e.strip())
        if engines:
            config.structured.table_engines = engines
    return PDFProcessingPipeline(config=config)
