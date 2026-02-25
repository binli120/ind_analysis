"""Upload/S3 API route declarations extracted from server."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from pdf_analysis.api.constants import UPLOAD_ROUTER_TAGS

def _sync_server_globals_dep() -> None:
    """Refresh server globals before each request."""
    _sync_server_globals()


upload_router = APIRouter(
    tags=UPLOAD_ROUTER_TAGS,
    dependencies=[Depends(_sync_server_globals_dep)],
)

def _sync_server_globals() -> None:
    """Pull shared helpers/types from server at import time."""
    from pdf_analysis.api import server as _server

    for key, value in _server.__dict__.items():
        if key.startswith("__"):
            continue
        if key in {"ncd_router", "dev_router", "upload_router"}:
            continue
        globals()[key] = value

_sync_server_globals()

@upload_router.post("/analyze", openapi_extra={"requestBody": _ANALYZE_REQUEST_BODY_OPENAPI})
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

    if (
        not content_type
        or "application/json" in content_type
        or "text/json" in content_type
    ):
        raw_body = await request.body()
        if not raw_body:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Request body is required. Use multipart/form-data with a 'file' field "
                    "or application/json with 'bucket' and 'key'."
                ),
            )
        try:
            data = json.loads(raw_body)
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail="Invalid JSON payload."
            ) from exc
        try:
            payload = S3AnalyzeRequest.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        return await _analyze_s3_payload(payload)

    raise HTTPException(
        status_code=415,
        detail="Unsupported media type. Use multipart/form-data for uploads or application/json for S3 analysis.",
    )


@upload_router.post("/s3/analyze")
async def analyze_s3(payload: S3AnalyzeRequest) -> Dict[str, Any]:
    """Analyze a PDF referenced by S3 bucket/key using a schema-stable JSON endpoint."""
    return await _analyze_s3_payload(payload)


@upload_router.post("/s3/upload-analyze")
async def upload_and_analyze_to_s3(
    file: UploadFile = File(...),
    bucket: str = Form(..., description="Destination S3 bucket for the uploaded PDF."),
    company: str = Form(
        ..., description="Top-level path segment (e.g. company domain)."
    ),
    project: str = Form(
        ..., description="Project identifier used when building the S3 key."
    ),
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
        raise HTTPException(
            status_code=400, detail="Unsupported table extraction engine."
        )

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

    s3_client = _boto3_client("s3", region_name=aws_region)
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
        _raise_sanitized_http_error(
            status_code=502,
            detail="Failed to upload to S3.",
            exc=exc,
            log_message="Failed to upload to S3",
        )

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
        _raise_sanitized_http_error(
            status_code=500,
            detail="Failed to schedule analysis.",
            exc=exc,
            log_message="Failed to schedule analysis",
        )

    def _log_async_failure(fut: asyncio.Future[Any]) -> None:
        """Log async failure."""
        try:
            fut.result()
        except asyncio.CancelledError:
            logger.warning(
                "Analysis task for s3://%s/%s was cancelled", bucket, object_key
            )
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
            _raise_sanitized_http_error(
                status_code=500,
                detail="Analysis failed.",
                exc=exc,
                log_message="S3 upload-and-analyze task failed",
            )

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
    """Execute the core extraction pipeline and return serialisable artefacts."""
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
        "pages_with_text": sum(1 for page in pages if (page.get("text") or "").strip()),
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
            float(cast(float, metrics_payload["pages_with_text"]))
            / float(cast(float, total_pages)),
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


def _extract_sample_text(pdf_path: Path, page_limit: int) -> Tuple[str, int]:
    """
    Grab minimal text from the first N pages using layered fallbacks.
    Returns (text, pages_sampled).
    """
    pages_sampled = 0
    sample_text = ""

    try:
        pages = extract_pages_text(
            pdf_path,
            ocr_fallback=False,
            max_pages=page_limit,
        )
        pages_sampled = len(pages)
        sample_text = "\n".join((p.get("text") or "") for p in pages)
        if not sample_text.strip():
            pages = extract_pages_text(
                pdf_path, ocr_fallback=True, max_pages=page_limit
            )
            pages_sampled = len(pages)
            sample_text = "\n".join((p.get("text") or "") for p in pages)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Primary text extraction failed: %s", exc)

    if sample_text.strip():
        return sample_text, pages_sampled

    # PyMuPDF fallback
    try:
        import fitz  # type: ignore

        doc = fitz.open(pdf_path)
        texts: List[str] = []
        for page in doc[:page_limit]:
            texts.append(page.get_text("text") or "")
        sample_text = "\n".join(texts)
        pages_sampled = max(pages_sampled, min(page_limit, len(doc)))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("fitz text extraction failed: %s", exc)

    if sample_text.strip():
        return sample_text, pages_sampled

    # pdfplumber fallback (best effort)
    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(pdf_path) as pdfdoc:
            texts = []
            for page in pdfdoc.pages[:page_limit]:
                texts.append(page.extract_text() or "")
            sample_text = "\n".join(texts)
            pages_sampled = max(pages_sampled, min(page_limit, len(pdfdoc.pages)))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("pdfplumber text extraction failed: %s", exc)

    return sample_text, pages_sampled


def _extract_rtf_text(rtf_path: Path) -> str:
    """Convert RTF to plain text using pandoc CLI; fall back to naive stripping."""
    try:
        result = subprocess.run(
            ["pandoc", str(rtf_path), "-t", "plain"],
            capture_output=True,
            text=True,
            check=True,
        )
        text = result.stdout
        if text.strip():
            return text
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("pandoc rtf->text failed: %s", exc)

    # Naive fallback: strip common RTF control words/braces.
    try:
        raw = rtf_path.read_text(errors="ignore")
    except Exception:
        return ""
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", raw)
    text = text.replace("{", " ").replace("}", " ")
    return text


def _extract_sample_text_generic(
    path: Path, *, page_limit: int, original_suffix: str
) -> Tuple[str, int]:
    """
    Extension-aware text sampler for PDF, DOCX, and RTF uploads.
    Returns (text, pages_sampled).
    """
    suffix = original_suffix.lower()
    if suffix == ".docx":
        try:
            pages = extract_docx_pages(path, max_pages=page_limit)
            text = "\n".join((p.get("text") or "") for p in pages)
            return text, len(pages)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("DOCX text extraction failed: %s", exc)

    if suffix == ".rtf":
        text = _extract_rtf_text(path)
        # Treat as a single logical page; keep parity with PDF behavior.
        return text, 1 if text else 0

    # Default / PDF path
    return _extract_sample_text(path, page_limit)


@upload_router.post("/s3/markdown")
async def fetch_s3_markdown(payload: S3MarkdownRequest) -> Dict[str, Any]:
    """Extract markdown directly from an S3 object and persist refreshed metadata."""
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
        """Worker."""
        pipeline = PDFProcessingPipeline()
        s3_client = _boto3_client("s3", region_name=payload.aws_region)
        with NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp_path = Path(tmp.name)
            try:
                if extra_args:
                    s3_client.download_fileobj(
                        payload.bucket, payload.key, tmp, ExtraArgs=extra_args
                    )
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
                logger.warning(
                    "Metadata generation failed for %s: %s", payload.key, exc
                )
                metadata_fields = {}
        metadata_fields.setdefault("analyzed", True)

        meta_payload = {
            "bucket": payload.bucket,
            "key": payload.key,
            "version_id": payload.version_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata_fields,
        }

        _update_object_metadata(
            s3_client, payload.bucket, payload.key, payload.version_id, metadata_fields
        )
        _upload_metadata_json_to_s3(
            s3_client, payload.bucket, payload.key, meta_payload
        )

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
        _raise_sanitized_http_error(
            status_code=502,
            detail="Failed to download S3 object.",
            exc=exc,
            log_message="Failed to download S3 object for markdown extraction",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@upload_router.post("/s3/markdown/summary")
async def fetch_s3_markdown_with_summary(payload: S3MarkdownRequest) -> Dict[str, Any]:
    """Return markdown plus an OpenAI-generated summary/topic listing for an S3 object."""
    if not _summary_generator or not _summary_generator.is_available():
        raise HTTPException(
            status_code=503,
            detail="OpenAI summary generator is not configured. Install infra extras and set OPENAI_API_KEY.",
        )
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise HTTPException(
            status_code=500,
            detail="boto3 is required for S3 summary extraction. Install the 'infra' extras.",
        ) from exc

    extra_args = {"VersionId": payload.version_id} if payload.version_id else None

    def worker() -> Dict[str, Any]:
        """Worker."""
        pipeline = PDFProcessingPipeline()
        s3_client = _boto3_client("s3", region_name=payload.aws_region)
        with NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp_path = Path(tmp.name)
            try:
                if extra_args:
                    s3_client.download_fileobj(
                        payload.bucket, payload.key, tmp, ExtraArgs=extra_args
                    )
                else:
                    s3_client.download_fileobj(payload.bucket, payload.key, tmp)
                tmp.flush()
            finally:
                tmp.close()

        try:
            result = pipeline.run(tmp_path)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

        if not result.markdown:
            raise RuntimeError("Markdown extraction failed for the specified object")

        summary_result = _summary_generator(result.markdown)
        if not summary_result:
            raise RuntimeError("Summary generation failed for the specified object")

        summary_text = format_summary_text(summary_result)
        markdown_with_topics = embed_topics_into_markdown(
            result.markdown, summary_result.topics
        )
        topics_payload = [
            {
                "title": topic.title,
                "description": topic.description,
                "anchor": topic.anchor,
            }
            for topic in summary_result.topics
        ]

        return {
            "markdown": markdown_with_topics,
            "summary_text": summary_text,
            "topics": topics_payload,
            "text_engine": result.text_engine,
            "ocr_strategy": result.ocr_strategy,
            "tables": len(result.tables),
            "version_id": payload.version_id,
            "bucket": payload.bucket,
            "key": payload.key,
        }

    try:
        return await asyncio.to_thread(worker)
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - boto specific
        _raise_sanitized_http_error(
            status_code=502,
            detail="Failed to download S3 object.",
            exc=exc,
            log_message="Failed to download S3 object for markdown summary extraction",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _build_markdown_key(path: str, filename: str) -> str:
    """Construct an S3 key for storing markdown drafts."""
    clean_path = path.strip("/")
    base_name = filename.strip()
    if not base_name:
        raise ValueError("filename must be provided")
    key = f"{clean_path}/{base_name}" if clean_path else base_name
    if not key.lower().endswith(".md"):
        key += ".md"
    return key


@upload_router.post("/s3/new-project")
async def create_s3_new_project(payload: S3NewProjectRequest) -> Dict[str, Any]:
    """Create a tenant/project folder scaffold in S3 from the configured template."""
    try:
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise HTTPException(
            status_code=500,
            detail="boto3 is required for S3 project creation. Install the 'infra' extras.",
        ) from exc

    tenant_name = _normalize_s3_path_segment(payload.tenant_name, "tenant_name")
    project_name = _normalize_s3_path_segment(payload.project_name, "project_name")
    base_prefix = f"{tenant_name}/{project_name}/"

    try:
        structure = _load_s3_folder_template()
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        _raise_sanitized_http_error(
            status_code=500,
            detail="Invalid S3 folder template.",
            exc=exc,
            log_message="Invalid S3 folder template",
        )

    folder_keys = [base_prefix, *_collect_s3_folder_keys(base_prefix, structure)]

    def worker() -> Dict[str, Any]:
        """Worker."""
        s3_client = _boto3_client("s3", region_name=payload.aws_region)
        for key in folder_keys:
            s3_client.put_object(Bucket=payload.bucket, Key=key)
        return {
            "status": "OK",
            "bucket": payload.bucket,
            "tenant_name": tenant_name,
            "project_name": project_name,
            "base_prefix": base_prefix,
            "created_folders": len(folder_keys),
        }

    try:
        return await asyncio.to_thread(worker)
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - boto specific
        _raise_sanitized_http_error(
            status_code=502,
            detail="Failed to create project folders.",
            exc=exc,
            log_message="Failed to create project folders",
        )


@upload_router.post("/s3/markdown/save")
async def save_s3_markdown(payload: S3MarkdownUploadRequest) -> Dict[str, Any]:
    """Save a markdown document to S3 while capturing metadata/tag sidecars."""
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
        tagging = urlencode(payload.tags)

    def worker() -> Dict[str, Any]:
        """Worker."""
        s3_client = _boto3_client("s3", region_name=payload.aws_region)
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
        _raise_sanitized_http_error(
            status_code=502,
            detail="Failed to upload markdown.",
            exc=exc,
            log_message="Failed to upload markdown",
        )


@upload_router.get("/s3/analysis/status")
async def get_s3_analysis_status(
    bucket: str,
    key: str,
    aws_region: Optional[str] = None,
) -> Dict[str, Any]:
    """Check whether the async analysis payload has been written back to S3."""
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
        """Worker."""
        s3_client = _boto3_client("s3", region_name=aws_region)
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
        _raise_sanitized_http_error(
            status_code=502,
            detail="Failed to check analysis status.",
            exc=exc,
            log_message="Failed to check analysis status",
        )


@upload_router.get("/s3/analysis/result")
async def get_s3_analysis_result(
    bucket: str,
    key: str,
    aws_region: Optional[str] = None,
) -> Dict[str, Any]:
    """Retrieve the persisted analysis JSON produced by the pipeline."""
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
        """Worker."""
        s3_client = _boto3_client("s3", region_name=aws_region)
        try:
            response = s3_client.get_object(Bucket=bucket, Key=analysis_key)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code in {"404", "NoSuchKey"}:
                raise HTTPException(
                    status_code=404, detail="Analysis result not found."
                )
            raise
        body_stream = response["Body"]
        try:
            payload = body_stream.read()
        finally:
            body_stream.close()
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            _raise_sanitized_http_error(
                status_code=500,
                detail="Stored analysis payload is invalid JSON.",
                exc=exc,
                log_message="Stored analysis payload is invalid JSON",
            )

    try:
        return await asyncio.to_thread(worker)
    except HTTPException:
        raise
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - boto specific
        _raise_sanitized_http_error(
            status_code=502,
            detail="Failed to download analysis result.",
            exc=exc,
            log_message="Failed to download analysis result",
        )
