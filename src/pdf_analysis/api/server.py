# @author: Bin Lee
# @email: blee@filynai.com

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Sequence, cast

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from pdf_analysis.ingest.pdf_text import extract_pages_text
from pdf_analysis.ingest.tables import extract_tables_all
from pdf_analysis.pipeline import PDFProcessingPipeline
from pdf_analysis.service.ai_metadata import OpenAIMetadataGenerator
from pdf_analysis.transform.markdown_writer import (
    build_html_document,
    build_markdown_document,
)
from pdf_analysis.validate import generate_quality_report

app = FastAPI(
    title="PDF Analysis API",
    description="Upload a PDF study report and receive extracted content, structured tables, and quality analysis.",
    version="0.1.0",
)

logger = logging.getLogger(__name__)


def _table_to_payload(
    table: Dict[str, Any], *, max_rows: Optional[int] = None
) -> Dict[str, Any]:
    df: pd.DataFrame = table["dataframe"].fillna("").astype(str)
    if max_rows is not None:
        df_preview = df.head(max_rows)
    else:
        df_preview = df
    return {
        "page_number": table["page_number"],
        "index_on_page": table["index_on_page"],
        "engine": table["engine"],
        "columns": [str(col) for col in df.columns],
        "rows": df_preview.to_dict(orient="records"),
        "row_count": int(df.shape[0]),
    }


def _build_table_manifest(
    tables: List[Dict[str, Any]], preview_rows: int = 10
) -> List[Dict[str, Any]]:
    manifest: List[Dict[str, Any]] = []
    for table in tables:
        df: pd.DataFrame = table["dataframe"].fillna("").astype(str)
        manifest.append(
            {
                "page_number": table["page_number"],
                "index_on_page": table["index_on_page"],
                "engine": table["engine"],
                "csv": None,
                "json": None,
                "preview_rows": df.head(preview_rows),
            }
        )
    return manifest


class S3AnalyzeRequest(BaseModel):
    bucket: str
    key: str
    version_id: Optional[str] = None
    aws_region: Optional[str] = None


class S3MarkdownRequest(BaseModel):
    bucket: str
    key: str
    version_id: Optional[str] = None
    aws_region: Optional[str] = None


class S3MarkdownUploadRequest(BaseModel):
    bucket: str
    path: str
    filename: str
    markdown: str
    label: Optional[str] = None
    tags: Optional[Dict[str, str]] = None
    metadata: Optional[Dict[str, str]] = None
    aws_region: Optional[str] = None


try:
    _metadata_generator = OpenAIMetadataGenerator()
except Exception:  # pragma: no cover - optional dependency or missing key
    _metadata_generator = None


def _metadata_json_key(key: str) -> str:
    return f"{key}.meta.json"


def _update_object_metadata(
    s3_client: Any,
    bucket: str,
    key: str,
    version_id: Optional[str],
    metadata_fields: Dict[str, Any],
) -> None:
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
    return f"{key}.analysis.json"


def _upload_analysis_json_to_s3(
    s3_client: Any,
    bucket: str,
    key: str,
    payload: Dict[str, Any],
) -> str:
    analysis_key = _analysis_json_key(key)
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=analysis_key,
            Body=json.dumps(payload, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Failed to upload analysis payload for %s: %s", analysis_key, exc)
    return analysis_key


def _normalise_table_engines(
    table_engine: str | Sequence[str] | None,
) -> List[str]:
    if table_engine is None:
        return ["pdfplumber", "camelot", "tabula"]
    if isinstance(table_engine, str):
        candidate = table_engine.strip().lower()
        if not candidate or candidate == "auto":
            return ["pdfplumber", "camelot", "tabula"]
        return [candidate]
    engines: List[str] = []
    for item in table_engine:
        if not item:
            continue
        value = item.strip().lower()
        if value and value not in engines:
            engines.append(value)
    return engines or ["pdfplumber", "camelot", "tabula"]


def _extract_tables_with_candidates(
    pdf_path: Path,
    candidate_engines: Sequence[str],
    max_pages: Optional[int],
) -> tuple[List[Dict[str, Any]], str]:
    best_tables: List[Dict[str, Any]] = []
    best_engine: Optional[str] = None
    best_count = -1
    for engine in candidate_engines:
        try:
            tables = extract_tables_all(pdf_path, engine=engine, max_pages=max_pages)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Table extraction failed via %s: %s", engine, exc)
            continue
        count = len(tables)
        if best_engine is None or count > best_count:
            best_tables = tables
            best_engine = engine
            best_count = count
    if best_engine is None:
        # all attempts failed; return empty list but preserve first candidate for downstream metadata
        best_engine = candidate_engines[0] if candidate_engines else "pdfplumber"
    return best_tables, best_engine


def _split_path_segments(value: str) -> List[str]:
    if not value:
        return []
    segments: List[str] = []
    for part in value.split("/"):
        candidate = part.strip()
        if not candidate or candidate in {".", ".."}:
            continue
        segments.append(candidate)
    return segments


def _build_pdf_s3_key(company: str, project: str, folder: str, filename: str) -> str:
    parts = _split_path_segments(company)
    if not parts:
        raise ValueError("company must be provided")
    project_parts = _split_path_segments(project)
    if not project_parts:
        raise ValueError("project must be provided")
    folder_parts = _split_path_segments(folder)
    basename = Path(filename).name
    if not basename.lower().endswith(".pdf"):
        raise ValueError("filename must end with .pdf")

    key_parts = parts + project_parts + folder_parts + [basename]
    return "/".join(key_parts)


def _process_pdf_and_store_analysis(
    pdf_path: Path,
    *,
    filename: str,
    bucket: str,
    key: str,
    version_id: Optional[str],
    aws_region: Optional[str],
    engine: str,
    max_pages: Optional[int],
    ocr_fallback: bool,
    table_rows: Optional[int],
    metadata_context: Dict[str, str],
) -> Dict[str, Any]:
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("boto3 is required for S3 operations. Install the 'infra' extras.") from exc

    s3_client = boto3.client("s3", region_name=aws_region)

    try:
        analysis_result = _run_pipeline_with_runner(
            pdf_path,
            filename,
            max_pages,
            engine,
            ocr_fallback,
            table_rows,
        )
    finally:
        try:
            pdf_path.unlink()
        except Exception:
            pass

    metadata_fields: Dict[str, Any] = {}
    if _metadata_generator and analysis_result.get("markdown"):
        try:
            metadata_fields = _metadata_generator(analysis_result["markdown"]) or {}
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("Metadata generation failed for %s: %s", key, exc)
            metadata_fields = {}
    metadata_fields.setdefault("analyzed", True)
    for meta_key, meta_value in metadata_context.items():
        if meta_value:
            metadata_fields.setdefault(meta_key, meta_value)

    generated_at = datetime.now(timezone.utc).isoformat()
    meta_payload = {
        "bucket": bucket,
        "key": key,
        "version_id": version_id,
        "generated_at": generated_at,
        "metadata": metadata_fields,
    }

    try:
        _update_object_metadata(s3_client, bucket, key, version_id, metadata_fields)
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - boto specific
        logger.warning("Failed to update metadata for %s: %s", key, exc)

    _upload_metadata_json_to_s3(s3_client, bucket, key, meta_payload)

    analysis_payload = {
        "generated_at": generated_at,
        "analysis": analysis_result,
        "metadata": metadata_fields,
        "markdown": analysis_result.get("markdown"),
        "s3": {
            "bucket": bucket,
            "key": key,
            "version_id": version_id,
            "metadata_key": _metadata_json_key(key),
            "analysis_key": _analysis_json_key(key),
        },
    }
    _upload_analysis_json_to_s3(s3_client, bucket, key, analysis_payload)
    return analysis_payload


async def _analyze_uploaded_pdf(upload: UploadFile) -> Dict[str, Any]:
    filename = upload.filename or "uploaded.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    with NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp_path = Path(tmp.name)
        try:
            shutil.copyfileobj(upload.file, tmp)
            tmp.flush()
        finally:
            upload.file.close()

    try:
        analysis = await asyncio.to_thread(
            _run_pipeline_with_runner,
            tmp_path,
            Path(filename).name,
            None,
            None,
            None,
            None,
        )
    except HTTPException:
        raise
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    markdown = analysis.get("markdown")
    metadata_fields: Dict[str, Any] = {}
    if _metadata_generator and markdown:
        try:
            metadata_fields = _metadata_generator(markdown) or {}
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("Metadata generation failed for %s: %s", filename, exc)
            metadata_fields = {}
    metadata_fields.setdefault("analyzed", True)

    if metadata_fields:
        analysis_with_metadata = dict(analysis)
        analysis_with_metadata["metadata"] = metadata_fields
        return analysis_with_metadata
    return analysis


async def _analyze_s3_payload(payload: S3AnalyzeRequest) -> Dict[str, Any]:
    try:  # Lazy import keeps base install lightweight.
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise HTTPException(
            status_code=500,
            detail="boto3 is required for S3 analysis. Install the 'infra' extras.",
        ) from exc

    extra_args = {"VersionId": payload.version_id} if payload.version_id else None

    def worker() -> Dict[str, Any]:
        s3_client = boto3.client("s3", region_name=payload.aws_region)
        with NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp_path = Path(tmp.name)
            try:
                if extra_args:
                    s3_client.download_fileobj(payload.bucket, payload.key, tmp, ExtraArgs=extra_args)
                else:
                    s3_client.download_fileobj(payload.bucket, payload.key, tmp)
                tmp.flush()
            finally:
                tmp.close()

        try:
            analysis = _run_pipeline_with_runner(
                tmp_path,
                Path(payload.key).name,
                max_pages=None,
                table_engine=None,
                ocr_fallback=None,
                table_rows=None,
            )
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass

        markdown = analysis.get("markdown")
        metadata_fields: Dict[str, Any] = {}
        if _metadata_generator and markdown:
            try:
                metadata_fields = _metadata_generator(markdown) or {}
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning(
                    "Metadata generation failed for %s: %s",
                    payload.key,
                    exc,
                )
                metadata_fields = {}
        metadata_fields.setdefault("analyzed", True)

        generated_at = datetime.now(timezone.utc).isoformat()
        meta_payload = {
            "bucket": payload.bucket,
            "key": payload.key,
            "version_id": payload.version_id,
            "generated_at": generated_at,
            "metadata": metadata_fields,
        }

        try:
            _update_object_metadata(
                s3_client,
                payload.bucket,
                payload.key,
                payload.version_id,
                metadata_fields,
            )
        except (BotoCoreError, ClientError) as exc:  # pragma: no cover - boto specific
            logger.warning("Failed to update metadata for %s: %s", payload.key, exc)

        _upload_metadata_json_to_s3(
            s3_client,
            payload.bucket,
            payload.key,
            meta_payload,
        )

        response_payload = {
            "markdown": markdown,
            "metadata": metadata_fields,
            "analysis": analysis,
            "s3": {
                "bucket": payload.bucket,
                "key": payload.key,
                "version_id": payload.version_id,
                "metadata_key": _metadata_json_key(payload.key),
            },
        }
        return response_payload

    try:
        return await asyncio.to_thread(worker)
    except HTTPException:
        raise
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - boto specific
        raise HTTPException(status_code=502, detail=f"Failed to download S3 object: {exc}") from exc


@app.post("/analyze")
async def analyze_pdf(request: Request) -> Dict[str, Any]:
    """
    Analyze a PDF from either a direct upload or an S3 location.
    """
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "file"):
            raise HTTPException(
                status_code=400,
                detail="The request must include a 'file' field containing a PDF document.",
            )
        return await _analyze_uploaded_pdf(cast(UploadFile, upload))

    if not content_type or "application/json" in content_type or "text/json" in content_type:
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
        try:
            payload = S3AnalyzeRequest.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        return await _analyze_s3_payload(payload)

    raise HTTPException(
        status_code=415,
        detail="Unsupported media type. Use multipart/form-data for uploads or application/json for S3 analysis.",
    )


@app.post("/s3/upload-analyze")
async def upload_and_analyze_to_s3(
    file: UploadFile = File(...),
    bucket: str = Form(..., description="Destination S3 bucket for the uploaded PDF."),
    company: str = Form(..., description="Top-level path segment (e.g. company domain)."),
    project: str = Form(..., description="Project identifier used when building the S3 key."),
    folder: str = Form(
        "",
        description="Optional nested folder (e.g. Module 1/Study Docs).",
    ),
    aws_region: Optional[str] = Form(
        None,
        description="AWS region for S3 operations; defaults to the SDK configuration.",
    ),
    wait_for_completion: bool = Form(
        True,
        description="Whether to wait for the analysis result (up to wait_timeout_seconds).",
    ),
    wait_timeout_seconds: float = Form(
        25.0,
        gt=0,
        description="Maximum seconds to wait for analysis before returning a pending response.",
    ),
    engine: str = Form(
        "pdfplumber",
        description="Table extraction engine to use.",
    ),
    max_pages: Optional[int] = Form(
        None,
        ge=1,
        description="Optional limit on pages processed during analysis.",
    ),
    ocr_fallback: bool = Form(
        False,
        description="Attempt OCR on pages with no extracted text.",
    ),
    table_rows: Optional[int] = Form(
        200,
        ge=1,
        description="Maximum number of rows returned per table in the response.",
    ),
) -> Any:
    """Upload a PDF to S3, trigger analysis, and return the results or an async handle."""
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise HTTPException(
            status_code=500,
            detail="boto3 is required for S3 uploads. Install the 'infra' extras.",
        ) from exc

    if engine not in {"pdfplumber", "camelot", "tabula"}:
        raise HTTPException(status_code=400, detail="Unsupported table extraction engine.")

    filename = file.filename or "uploaded.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    folder_clean = "/".join(_split_path_segments(folder))
    metadata_context = {
        "company": company.strip(),
        "project": project.strip(),
        "folder": folder_clean,
    }
    metadata_context["path"] = "/".join(
        filter(
            None,
            _split_path_segments(company)
            + _split_path_segments(project)
            + _split_path_segments(folder),
        )
    )

    try:
        object_key = _build_pdf_s3_key(company, project, folder, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp_path = Path(tmp.name)
        try:
            shutil.copyfileobj(file.file, tmp)
            tmp.flush()
        finally:
            file.file.close()

    object_metadata = {
        key: value for key, value in metadata_context.items() if key != "path" and value
    }

    s3_client = boto3.client("s3", region_name=aws_region)
    try:
        with tmp_path.open("rb") as payload:
            put_response = s3_client.put_object(
                Bucket=bucket,
                Key=object_key,
                Body=payload,
                ContentType="application/pdf",
                Metadata={k: str(v) for k, v in object_metadata.items()},
            )
    except (BotoCoreError, ClientError) as exc:
        try:
            tmp_path.unlink()
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"Failed to upload to S3: {exc}") from exc

    version_id = put_response.get("VersionId")

    loop = asyncio.get_running_loop()
    try:
        analysis_future = asyncio.ensure_future(
            loop.run_in_executor(
                None,
                lambda: _process_pdf_and_store_analysis(
                    tmp_path,
                    filename=filename,
                    bucket=bucket,
                    key=object_key,
                    version_id=version_id,
                    aws_region=aws_region,
                    engine=engine,
                    max_pages=max_pages,
                    ocr_fallback=ocr_fallback,
                    table_rows=table_rows,
                    metadata_context=metadata_context,
                ),
            )
        )
    except Exception as exc:  # pragma: no cover - defensive
        try:
            tmp_path.unlink()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Failed to schedule analysis: {exc}") from exc

    def _log_async_failure(fut: asyncio.Future[Any]) -> None:
        try:
            fut.result()
        except asyncio.CancelledError:
            logger.warning("Analysis task for s3://%s/%s was cancelled", bucket, object_key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "Background analysis failed for s3://%s/%s: %s",
                bucket,
                object_key,
                exc,
                exc_info=exc,
            )

    analysis_future.add_done_callback(_log_async_failure)

    # API Gateway enforces a 29s limit; keep some headroom.
    effective_timeout = min(wait_timeout_seconds, 28.0)

    if wait_for_completion:
        try:
            analysis_payload = await asyncio.wait_for(
                asyncio.shield(analysis_future),
                timeout=effective_timeout,
            )
            return {
                "status": "completed",
                "markdown": analysis_payload.get("markdown"),
                "metadata": analysis_payload.get("metadata"),
                "s3": analysis_payload.get("s3"),
                "analysis": analysis_payload.get("analysis"),
                "generated_at": analysis_payload.get("generated_at"),
            }
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc

    pending_payload = {
        "status": "pending",
        "s3": {
            "bucket": bucket,
            "key": object_key,
            "version_id": version_id,
            "metadata_key": _metadata_json_key(object_key),
            "analysis_key": _analysis_json_key(object_key),
        },
        "metadata": metadata_context,
        "message": "Analysis is running asynchronously. Poll the status or result endpoints.",
        "status_url": f"/s3/analysis/status?bucket={bucket}&key={object_key}",
        "result_url": f"/s3/analysis/result?bucket={bucket}&key={object_key}",
    }
    return JSONResponse(status_code=202, content=pending_payload)


def _run_pipeline_with_runner(
    pdf_path: Path,
    filename: str,
    max_pages: Optional[int],
    table_engine: str | Sequence[str] | None,
    ocr_fallback: Optional[bool],
    table_rows: Optional[int],
) -> Dict[str, Any]:
    candidate_engines = _normalise_table_engines(table_engine)

    fallback_setting = bool(ocr_fallback) if ocr_fallback is not None else False
    pages = extract_pages_text(
        pdf_path,
        ocr_fallback=fallback_setting,
        max_pages=max_pages,
    )

    used_ocr = fallback_setting
    if ocr_fallback is None:
        has_text = any((page.get("text") or "").strip() for page in pages)
        if not has_text:
            pages = extract_pages_text(
                pdf_path,
                ocr_fallback=True,
                max_pages=max_pages,
            )
            used_ocr = True

    tables, selected_engine = _extract_tables_with_candidates(
        pdf_path,
        candidate_engines,
        max_pages,
    )

    table_manifest = _build_table_manifest(tables)
    markdown = build_markdown_document(filename, pages, table_manifest)
    html = build_html_document(filename, pages, table_manifest)

    quality = generate_quality_report(
        pdf_path,
        pages,
        tables,
        extraction_limit=max_pages,
    )

    tables_payload = [_table_to_payload(t, max_rows=table_rows) for t in tables]

    metrics_payload = {
        "total_pages": len(pages),
        "pages_with_text": sum(
            1 for page in pages if (page.get("text") or "").strip()
        ),
        "tables_total": len(tables),
        "table_pages": len(
            {table["page_number"] for table in tables if table.get("page_number")}
        ),
        "ocr_pages": 0,
        "ocr_enabled": used_ocr,
        "key_value_pairs": 0,
        "text_coverage": 0.0,
        "confidence": None,
        "table_engine": selected_engine,
        "table_engines_considered": candidate_engines,
    }
    total_pages = metrics_payload["total_pages"]
    if total_pages:
        metrics_payload["text_coverage"] = round(
            float(cast(float, metrics_payload["pages_with_text"])) /
            float(cast(float, total_pages)),
            3,
        )

    return {
        "document": filename,
        "pages": pages,
        "markdown": markdown,
        "html": html,
        "tables": tables_payload,
        "quality": quality.get("json", {}) if quality else {},
        "quality_markdown": quality.get("markdown") if quality else None,
        "metrics": metrics_payload,
        "selected_table_engine": selected_engine,
    }


@app.post("/s3/markdown")
async def fetch_s3_markdown(payload: S3MarkdownRequest) -> Dict[str, Any]:
    try:  # Lazy import so API works without S3 extras.
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise HTTPException(
            status_code=500,
            detail="boto3 is required for S3 markdown extraction. Install the 'infra' extras.",
        ) from exc

    extra_args = {"VersionId": payload.version_id} if payload.version_id else None

    def worker() -> Dict[str, Any]:
        pipeline = PDFProcessingPipeline()
        s3_client = boto3.client("s3", region_name=payload.aws_region)
        with NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp_path = Path(tmp.name)
            try:
                if extra_args:
                    s3_client.download_fileobj(payload.bucket, payload.key, tmp, ExtraArgs=extra_args)
                else:
                    s3_client.download_fileobj(payload.bucket, payload.key, tmp)
                tmp.flush()
            finally:
                tmp.close()

        try:
            result = pipeline.run(tmp_path)
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass

        if not result.markdown:
            raise RuntimeError("Markdown extraction failed for the specified object")

        metadata_fields: Dict[str, Any] = {}
        if _metadata_generator and result.markdown:
            try:
                metadata_fields = _metadata_generator(result.markdown) or {}
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("Metadata generation failed for %s: %s", payload.key, exc)
                metadata_fields = {}
        metadata_fields.setdefault("analyzed", True)

        meta_payload = {
            "bucket": payload.bucket,
            "key": payload.key,
            "version_id": payload.version_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata_fields,
        }

        _update_object_metadata(s3_client, payload.bucket, payload.key, payload.version_id, metadata_fields)
        _upload_metadata_json_to_s3(s3_client, payload.bucket, payload.key, meta_payload)

        return {
            "markdown": result.markdown,
            "text_engine": result.text_engine,
            "ocr_strategy": result.ocr_strategy,
            "tables": len(result.tables),
            "metadata": metadata_fields,
            "version_id": payload.version_id,
            "bucket": payload.bucket,
            "key": payload.key,
        }

    try:
        return await asyncio.to_thread(worker)
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - boto specific
        raise HTTPException(status_code=502, detail=f"Failed to download S3 object: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _build_markdown_key(path: str, filename: str) -> str:
    clean_path = path.strip("/")
    base_name = filename.strip()
    if not base_name:
        raise ValueError("filename must be provided")
    key = f"{clean_path}/{base_name}" if clean_path else base_name
    if not key.lower().endswith(".md"):
        key += ".md"
    return key


@app.post("/s3/markdown/save")
async def save_s3_markdown(payload: S3MarkdownUploadRequest) -> Dict[str, Any]:
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise HTTPException(
            status_code=500,
            detail="boto3 is required for saving markdown. Install the 'infra' extras.",
        ) from exc

    try:
        key = _build_markdown_key(payload.path, payload.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    metadata = payload.metadata.copy() if payload.metadata else {}
    if payload.label:
        metadata.setdefault("label", payload.label)

    tagging = ""
    if payload.tags:
        tagging = "&".join(f"{k}={v}" for k, v in payload.tags.items())

    def worker() -> Dict[str, Any]:
        s3_client = boto3.client("s3", region_name=payload.aws_region)
        kwargs: Dict[str, Any] = {
            "Bucket": payload.bucket,
            "Key": key,
            "Body": payload.markdown.encode("utf-8"),
            "ContentType": "text/markdown",
            "Metadata": metadata,
        }
        if tagging:
            kwargs["Tagging"] = tagging
        response = s3_client.put_object(**kwargs)
        version_id = response.get("VersionId")
        meta_payload = {
            "bucket": payload.bucket,
            "key": key,
            "version_id": version_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata,
            "label": payload.label,
            "tags": payload.tags or {},
        }
        _upload_metadata_json_to_s3(s3_client, payload.bucket, key, meta_payload)
        return {
            "bucket": payload.bucket,
            "key": key,
            "version_id": version_id,
            "label": payload.label,
            "tags": payload.tags or {},
            "metadata": metadata,
            "markdown": payload.markdown,
        }

    try:
        return await asyncio.to_thread(worker)
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - boto specific
        raise HTTPException(status_code=502, detail=f"Failed to upload markdown: {exc}") from exc


@app.get("/s3/analysis/status")
async def get_s3_analysis_status(
    bucket: str,
    key: str,
    aws_region: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise HTTPException(
            status_code=500,
            detail="boto3 is required for S3 analysis status checks. Install the 'infra' extras.",
        ) from exc

    analysis_key = _analysis_json_key(key)

    def worker() -> Dict[str, Any]:
        s3_client = boto3.client("s3", region_name=aws_region)
        try:
            head = s3_client.head_object(Bucket=bucket, Key=analysis_key)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code in {"404", "NoSuchKey"}:
                return {
                    "status": "pending",
                    "analysis_key": analysis_key,
                }
            raise

        last_modified = head.get("LastModified")
        last_modified_iso = (
            last_modified.astimezone(timezone.utc).isoformat()
            if isinstance(last_modified, datetime)
            else None
        )
        return {
            "status": "completed",
            "analysis_key": analysis_key,
            "last_modified": last_modified_iso,
            "content_length": head.get("ContentLength"),
        }

    try:
        return await asyncio.to_thread(worker)
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - boto specific
        raise HTTPException(status_code=502, detail=f"Failed to check analysis status: {exc}") from exc


@app.get("/s3/analysis/result")
async def get_s3_analysis_result(
    bucket: str,
    key: str,
    aws_region: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise HTTPException(
            status_code=500,
            detail="boto3 is required for S3 analysis retrieval. Install the 'infra' extras.",
        ) from exc

    analysis_key = _analysis_json_key(key)

    def worker() -> Dict[str, Any]:
        s3_client = boto3.client("s3", region_name=aws_region)
        try:
            response = s3_client.get_object(Bucket=bucket, Key=analysis_key)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code in {"404", "NoSuchKey"}:
                raise HTTPException(status_code=404, detail="Analysis result not found.")
            raise
        body_stream = response["Body"]
        try:
            payload = body_stream.read()
        finally:
            body_stream.close()
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=500, detail=f"Stored analysis payload is invalid JSON: {exc}"
            ) from exc

    try:
        return await asyncio.to_thread(worker)
    except HTTPException:
        raise
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - boto specific
        raise HTTPException(status_code=502, detail=f"Failed to download analysis result: {exc}") from exc

def export_openapi_to_file(app: FastAPI, out_path: str | Path) -> None:
    """Write the OpenAPI spec to JSON (or YAML)."""
    openapi_dict = app.openapi()
    out_path = Path(out_path)

    # JSON
    if out_path.suffix in {".json", ".yml", ".yaml"}:
        data = json.dumps(openapi_dict, indent=2)
    else:
        raise ValueError("Supported extensions: .json, .yml, .yaml")

    out_path.write_text(data, encoding="utf-8")
    print(f"✅ OpenAPI spec written to {out_path}")
