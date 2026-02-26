"""NCD + dev API route declarations extracted from server."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from pdf_analysis.api.constants import (
    DEV_ROUTER_PREFIX,
    DEV_ROUTER_TAGS,
    NCD_ROUTER_PREFIX,
    NCD_ROUTER_TAGS,
)
from pdf_analysis.constants.metadata_keys import (
    CLASSIFICATION_METHOD_KEY,
    IND_CLASSIFICATION_CONFIDENCE_KEY,
    IND_SECTION_NUMBER_KEY,
    IND_SECTION_TITLE_KEY,
    LABELS_KEY,
    PAGES_SAMPLED_KEY,
    SOURCE_KEY_KEY,
)

def _sync_server_globals_dep() -> None:
    """Refresh server globals before each request."""
    _sync_server_globals()


ncd_router = APIRouter(
    prefix=NCD_ROUTER_PREFIX,
    tags=NCD_ROUTER_TAGS,
    dependencies=[Depends(_sync_server_globals_dep)],
)
dev_router = APIRouter(
    prefix=DEV_ROUTER_PREFIX,
    tags=DEV_ROUTER_TAGS,
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


def _classify_by_scope(
    *,
    sample_text: str,
    filename: str,
    section_scope: str,
    use_llm: bool,
) -> tuple[Optional[Tuple[str, str, float, str]], List[Dict[str, Any]]]:
    """Classify sample text against IND/CTD templates for the requested scope."""
    scope = (section_scope or "auto").strip().lower()
    if scope not in {"auto", "ind", "ctd"}:
        raise HTTPException(
            status_code=400,
            detail="section_scope must be one of: auto, ind, ctd.",
        )

    ind_sections = _load_ind_template_sections()
    ctd_sections = _load_ctd_section_sections()

    if scope == "ind":
        classification = _classify_section_from_text(
            sample_text,
            filename=filename,
            use_llm=use_llm,
            sections=ind_sections,
        )
        return classification, ind_sections

    if scope == "ctd":
        classification = _classify_section_from_text(
            sample_text,
            filename=filename,
            use_llm=use_llm,
            sections=ctd_sections,
        )
        return classification, ctd_sections

    ind_class = _classify_section_from_text(
        sample_text,
        filename=filename,
        use_llm=use_llm,
        sections=ind_sections,
    )
    ctd_class = _classify_section_from_text(
        sample_text,
        filename=filename,
        use_llm=use_llm,
        sections=ctd_sections,
    )
    return _select_label_classification(
        ind_class=ind_class,
        ctd_class=ctd_class,
        ind_sections=ind_sections,
        ctd_sections=ctd_sections,
    )


@ncd_router.post("/label")
async def label_s3_pdf(payload: NCDLabelRequest) -> Dict[str, Any]:
    """
    Lightweight section labeling for a PDF stored in S3.

    - Downloads once, reads the first few pages (page_limit) to classify.
    - Uses template-driven heuristics with optional LLM refinement.
    - Copies the PDF into company/project/<section>/filename and updates metadata + sidecar.
    """
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise HTTPException(
            status_code=500,
            detail="boto3 is required for S3 labeling. Install the 'infra' extras.",
        ) from exc

    bucket = payload.bucket or os.getenv("S3_BUCKET")
    if not bucket:
        raise HTTPException(
            status_code=400,
            detail="bucket is required (pass in payload or set S3_BUCKET).",
        )

    page_limit = (
        payload.page_limit if payload.page_limit and payload.page_limit > 0 else 5
    )

    def worker() -> Dict[str, Any]:
        """Worker."""
        s3_client = _boto3_client("s3", region_name=payload.aws_region)
        with NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp_path = Path(tmp.name)
        try:
            with tmp_path.open("wb") as handle:
                s3_client.download_fileobj(bucket, payload.key, handle)
        except ClientError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            error_code = exc.response.get("Error", {}).get("Code")
            status = 404 if error_code in {"404", "NoSuchKey"} else 502
            if status == 404:
                _raise_sanitized_http_error(
                    status_code=404,
                    detail="S3 object not found.",
                    exc=exc,
                    log_message="Failed to fetch S3 object: object not found",
                )
            _raise_sanitized_http_error(
                status_code=502,
                detail="Failed to fetch S3 object.",
                exc=exc,
                log_message="Failed to fetch S3 object",
            )

        try:
            sample_text, pages_sampled = _extract_sample_text(tmp_path, page_limit)
            classification, _ = _classify_by_scope(
                sample_text=sample_text,
                filename=Path(payload.key).name,
                section_scope=payload.section_scope,
                use_llm=payload.use_llm,
            )
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass

        section_number = classification[0] if classification else None
        section_title = classification[1] if classification else None
        confidence = classification[2] if classification else 0.0
        method = classification[3] if classification else "none"

        target_folder = section_number or "unlabeled"
        key_parts = (
            _split_path_segments(payload.company)
            + _split_path_segments(payload.project)
            + _split_path_segments(target_folder)
        )
        if not key_parts:
            raise HTTPException(
                status_code=400, detail="company/project must be provided for labeling."
            )

        dest_key = "/".join(key_parts + [Path(payload.key).name])
        copy_source: Dict[str, Any] = {"Bucket": bucket, "Key": payload.key}

        try:
            copy_resp = s3_client.copy_object(
                Bucket=bucket,
                Key=dest_key,
                CopySource=copy_source,
                MetadataDirective="COPY",
            )
        except (BotoCoreError, ClientError) as exc:  # pragma: no cover - boto specific
            _raise_sanitized_http_error(
                status_code=502,
                detail="Failed to copy PDF to labeled folder.",
                exc=exc,
                log_message="Failed to copy PDF to labeled folder",
            )

        dest_version_id = copy_resp.get("VersionId")
        meta_fields: Dict[str, Any] = {
            IND_SECTION_NUMBER_KEY: section_number,
            IND_SECTION_TITLE_KEY: section_title,
            IND_CLASSIFICATION_CONFIDENCE_KEY: confidence,
            CLASSIFICATION_METHOD_KEY: method,
            SOURCE_KEY_KEY: payload.key,
            PAGES_SAMPLED_KEY: pages_sampled,
        }
        if section_number:
            meta_fields[LABELS_KEY] = [f"section:{section_number}"]

        _update_object_metadata(
            s3_client,
            bucket=bucket,
            key=dest_key,
            version_id=dest_version_id,
            metadata_fields=meta_fields,
        )
        _upload_metadata_json_to_s3(
            s3_client,
            bucket=bucket,
            key=dest_key,
            payload=meta_fields,
        )

        return {
            "section_number": section_number,
            "section_title": section_title,
            "confidence": confidence,
            "method": method,
            "pages_sampled": pages_sampled,
            "s3": {
                "bucket": bucket,
                "source_key": payload.key,
                "labeled_key": dest_key,
                "version_id": dest_version_id,
                "metadata_key": _metadata_json_key(dest_key),
            },
        }

    try:
        return await asyncio.to_thread(worker)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        _raise_sanitized_http_error(
            status_code=500,
            detail="Labeling failed.",
            exc=exc,
            log_message="Unexpected labeling failure",
        )


@ncd_router.post("/label-upload")
async def label_uploaded_document(
    file: UploadFile = File(..., description="PDF, RTF, or Word (DOCX) file to label"),
    page_limit: int = Form(5, ge=1, description="Max pages to sample for labeling"),
    use_llm: bool = Form(False, description="Enable optional LLM refinement"),
    section_scope: str = Form(
        "auto",
        description="Label scope: ind (2.4/2.6), ctd (sectionList), or auto.",
    ),
) -> Dict[str, Any]:
    """
    Label an uploaded document without using S3.

    Accepts PDF, DOCX (Word), or RTF uploads and returns the predicted section
    number/title with confidence and top candidates. No S3 side-effects.
    """
    filename = file.filename or "uploaded.pdf"
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".docx", ".rtf"}:
        raise HTTPException(
            status_code=400,
            detail="Only PDF, DOCX (Word), or RTF files are supported for labeling.",
        )

    # Preserve a meaningful suffix for downstream parsers.
    tmp_suffix = suffix if suffix in {".pdf", ".docx"} else ".rtf"
    with NamedTemporaryFile(delete=False, suffix=tmp_suffix) as tmp:
        tmp_path = Path(tmp.name)
        try:
            shutil.copyfileobj(file.file, tmp)
            tmp.flush()
        finally:
            file.file.close()

    try:
        sample_text, pages_sampled = _extract_sample_text_generic(
            tmp_path, page_limit=page_limit, original_suffix=suffix
        )
        classification, candidate_sections = _classify_by_scope(
            sample_text=sample_text,
            filename=filename,
            section_scope=section_scope,
            use_llm=use_llm,
        )

        candidates = _top_section_candidates(
            sample_text, candidate_sections, limit=5
        )
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    if classification:
        section_number, section_title, confidence, method = classification
    else:
        section_number = section_title = None
        confidence = 0.0
        method = "none"

    if not candidates and section_number:
        candidates = [
            {
                "section_number": section_number,
                "section_title": section_title or section_number,
                "score": confidence,
            }
        ]

    return {
        "filename": filename,
        "file_type": suffix.lstrip("."),
        "section_number": section_number,
        "section_title": section_title,
        "confidence": confidence,
        "method": method,
        "pages_sampled": pages_sampled,
        "candidates": candidates,
    }


@ncd_router.post("/relabel")
async def relabel_s3_pdf(payload: NCDRelabelRequest) -> Dict[str, Any]:
    """
    Correct the section number/title for an S3 PDF by copying into the desired section folder
    and updating metadata sidecars.
    """
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise HTTPException(
            status_code=500,
            detail="boto3 is required for S3 relabel. Install the 'infra' extras.",
        ) from exc

    bucket = payload.bucket or os.getenv("S3_BUCKET")
    if not bucket:
        raise HTTPException(
            status_code=400,
            detail="bucket is required (pass in payload or set S3_BUCKET).",
        )

    target_folder = payload.section_number or "unlabeled"
    key_parts = (
        _split_path_segments(payload.company)
        + _split_path_segments(payload.project)
        + _split_path_segments(target_folder)
    )
    if not key_parts:
        raise HTTPException(
            status_code=400, detail="company/project must be provided for relabeling."
        )

    dest_key = "/".join(key_parts + [Path(payload.key).name])

    s3_client = _boto3_client("s3", region_name=payload.aws_region)
    copy_source: Dict[str, Any] = {"Bucket": bucket, "Key": payload.key}

    try:
        copy_resp = s3_client.copy_object(
            Bucket=bucket,
            Key=dest_key,
            CopySource=copy_source,
            MetadataDirective="COPY",
        )
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - boto specific
        _raise_sanitized_http_error(
            status_code=502,
            detail="Failed to copy PDF to relabeled folder.",
            exc=exc,
            log_message="Failed to copy PDF to relabeled folder",
        )

    # Best-effort move: delete the original after successful copy
    try:
        s3_client.delete_object(Bucket=bucket, Key=payload.key)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning(
            "Failed to delete original after relabel (%s): %s", payload.key, exc
        )

    dest_version_id = copy_resp.get("VersionId")
    meta_fields: Dict[str, Any] = {
        IND_SECTION_NUMBER_KEY: payload.section_number,
        IND_SECTION_TITLE_KEY: payload.section_title or payload.section_number,
        CLASSIFICATION_METHOD_KEY: "manual",
        SOURCE_KEY_KEY: payload.key,
        LABELS_KEY: [f"section:{payload.section_number}"],
    }

    _update_object_metadata(
        s3_client,
        bucket=bucket,
        key=dest_key,
        version_id=dest_version_id,
        metadata_fields=meta_fields,
    )
    _upload_metadata_json_to_s3(
        s3_client,
        bucket=bucket,
        key=dest_key,
        payload=meta_fields,
    )

    return {
        "section_number": payload.section_number,
        "section_title": meta_fields[IND_SECTION_TITLE_KEY],
        "method": "manual",
        "s3": {
            "bucket": bucket,
            "source_key": payload.key,
            "relabeled_key": dest_key,
            "version_id": dest_version_id,
            "metadata_key": _metadata_json_key(dest_key),
        },
    }


@dev_router.post("/label-local")
async def dev_label_local(
    file: UploadFile = File(...),
    page_limit: int = Form(5, ge=1, description="Max pages to sample for labeling"),
    use_llm: bool = Form(False, description="Enable optional LLM refinement"),
    section_scope: str = Form(
        "auto",
        description="Label scope: ind (2.4/2.6), ctd (sectionList), or auto.",
    ),
) -> Dict[str, Any]:
    """
    Dev-only: label a local document (PDF/DOCX/RTF) upload without S3 side-effects.
    Returns the predicted section, confidence, method, and top fuzzy candidates.
    """
    filename = file.filename or "uploaded.pdf"
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".docx", ".rtf"}:
        raise HTTPException(
            status_code=400,
            detail="Only PDF, DOCX (Word), or RTF files are supported.",
        )

    tmp_suffix = suffix if suffix in {".pdf", ".docx"} else ".rtf"
    with NamedTemporaryFile(delete=False, suffix=tmp_suffix) as tmp:
        tmp_path = Path(tmp.name)
        try:
            shutil.copyfileobj(file.file, tmp)
            tmp.flush()
        finally:
            file.file.close()

    try:
        sample_text, pages_sampled = _extract_sample_text_generic(
            tmp_path, page_limit=page_limit, original_suffix=suffix
        )
        classification, candidate_sections = _classify_by_scope(
            sample_text=sample_text,
            filename=filename,
            section_scope=section_scope,
            use_llm=use_llm,
        )

        candidates = _top_section_candidates(
            sample_text, candidate_sections, limit=5
        )
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    if classification:
        section_number, section_title, confidence, method = classification
    else:
        section_number = section_title = None
        confidence = 0.0
        method = "none"

    if not candidates and section_number:
        candidates = [
            {
                "section_number": section_number,
                "section_title": section_title or section_number,
                "score": confidence,
            }
        ]

    return {
        "filename": filename,
        "section_number": section_number,
        "section_title": section_title,
        "confidence": confidence,
        "method": method,
        "pages_sampled": pages_sampled,
        "candidates": candidates,
    }


@dev_router.get("/sections")
async def dev_list_sections(limit: int = 0) -> Dict[str, Any]:
    """
    Dev-only: return the known template sections for quick QA.
    """
    sections = _load_ind_template_sections()
    payload = sections if limit <= 0 else sections[:limit]
    return {"count": len(sections), "sections": payload}


@ncd_router.get("/sectionList")
async def ncd_section_list() -> Dict[str, Any]:
    """
    Return the hierarchical CTD/IND section list from sectionList.json.
    """
    try:
        sections = _load_section_list()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="sectionList.json not found")
    except ValueError as exc:
        _raise_sanitized_http_error(
            status_code=500,
            detail="Failed to load section list.",
            exc=exc,
            log_message="Failed to load section list",
        )

    return {"count": len(sections), "sections": sections}


@ncd_router.post("/template/override")
async def upsert_template_override(payload: TemplateOverrideRequest) -> Dict[str, Any]:
    """
    Create or update a user-specific template override for a section/subsection.
    """
    db = SessionLocal()
    try:
        record = _upsert_template_override(
            db,
            user_id=payload.user_id,
            section=payload.section.strip(),
            subsection=(payload.subsection or None),
            payload=payload.payload,
        )
        return record
    except Exception as exc:
        _raise_sanitized_http_error(
            status_code=500,
            detail="Failed to upsert template override.",
            exc=exc,
            log_message="Failed to upsert template override",
        )
    finally:
        db.close()


@ncd_router.get("/template")
async def get_template_sections(
    section: Optional[str] = None,
    user_id: str = Header(
        ...,
        alias="user-id",
        convert_underscores=False,
        description="Authenticated user ID (UUID) required in header",
    ),
) -> Dict[str, Any]:
    """
    Return template entries for a given section or subsection from ind_24_26_template.json.
    - If section is omitted, returns the full template JSON payload.
    - If you pass a subsection (e.g., 2.4.1-A), returns the matching entry.
    - If you pass a parent section (e.g., 2.4.1), returns all subsection entries under it.
    - If user_id is provided and overrides exist, they are merged (override wins).
    """
    def worker() -> Dict[str, Any]:
        """Worker."""
        db = SessionLocal()
        try:
            normalized_user_id = _require_valid_user_id(db, user_id)
            if not section or not section.strip():
                template_path = resolve_template_path()
                if not template_path:
                    raise HTTPException(status_code=404, detail="Template file not found")
                try:
                    payload = json.loads(template_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    _raise_sanitized_http_error(
                        status_code=500,
                        detail="Template file is invalid JSON.",
                        exc=exc,
                        log_message="Template file is invalid JSON",
                    )
                return {"template": payload, "user_id": normalized_user_id}

            target = section.strip()
            entries = _load_ind_template_entries()
            matches: List[Dict[str, Any]] = []
            target_lower = target.lower()
            target_element = normalize_element_number(target)
            for entry in entries:
                sec = (entry.get("section") or "").lower()
                sub = (entry.get("subsection") or "").lower()
                element_number = (entry.get("element_number") or "").lower()
                if element_number and target_element and element_number == target_element:
                    matches.append(entry)
                elif sub and sub == target_lower:
                    matches.append(entry)
                elif sec and sec == target_lower:
                    matches.append(entry)
                elif sec and sec.startswith(target_lower):
                    matches.append(entry)

            if not matches:
                raise HTTPException(status_code=404, detail="Section not found in template")

            overrides = _fetch_template_overrides(db, normalized_user_id, target)
            if overrides:
                override_map = {
                    ((o.get("section") or "").lower(), (o.get("subsection") or None)): o[
                        "payload"
                    ]
                    for o in overrides
                }
                merged: List[Dict[str, Any]] = []
                seen_keys = set()
                for entry in matches:
                    key = (
                        (entry.get("section") or "").lower(),
                        (entry.get("subsection") or None),
                    )
                    if key in override_map:
                        merged_entry = dict(entry)
                        merged_entry["raw"] = {
                            **(entry.get("raw") or {}),
                            **(override_map[key] or {}),
                        }
                        merged.append(merged_entry)
                        seen_keys.add(key)
                    else:
                        merged.append(entry)
                        seen_keys.add(key)
                for key, payload in override_map.items():
                    if key not in seen_keys:
                        merged.append(
                            {
                                "section": key[0],
                                "subsection": key[1],
                                "section_header": payload.get("Section Header"),
                                "subsection_header": payload.get("Subsection Header"),
                                "content": payload.get("Content"),
                                "raw": payload,
                            }
                        )
                matches = merged

            return {"section": target, "entries": matches, "user_id": normalized_user_id}
        finally:
            db.close()

    return await asyncio.to_thread(worker)


@ncd_router.get("/templates")
async def list_template_downloads(
    user_id: str = Header(
        ...,
        alias="user-id",
        convert_underscores=False,
        description="Authenticated user ID (UUID) required in header",
    ),
    bucket: Optional[str] = None,
    prefixes: Optional[str] = None,
    expires_in: int = 3600,
    aws_region: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return downloadable DOCX template URLs for the configured templates bucket.
    """
    db = SessionLocal()
    try:
        user_id = _require_valid_user_id(db, user_id)
    finally:
        db.close()

    target_bucket = bucket or DEFAULT_TEMPLATE_BUCKET
    if not target_bucket:
        raise HTTPException(status_code=400, detail="bucket is required")

    if expires_in <= 0:
        raise HTTPException(status_code=400, detail="expires_in must be positive")

    s3_client = _boto3_client("s3", region_name=aws_region)
    prefix_list = _normalize_prefix_list(prefixes)
    objects = _list_template_objects(s3_client, target_bucket, prefix_list)

    templates: List[Dict[str, Any]] = []
    for obj in objects:
        key = obj.get("Key")
        if not key:
            continue
        try:
            download_url = s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": target_bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except Exception as exc:  # pragma: no cover - network or auth errors
            logger.warning("Failed to presign s3://%s/%s: %s", target_bucket, key, exc)
            download_url = None
        last_modified = obj.get("LastModified")
        templates.append(
            {
                "name": Path(key).name,
                "s3_bucket": target_bucket,
                "s3_key": key,
                "s3_uri": f"s3://{target_bucket}/{key}",
                "download_url": download_url,
                "size_bytes": obj.get("Size"),
                "last_modified": (
                    last_modified.isoformat()
                    if isinstance(last_modified, datetime)
                    else None
                ),
            }
        )

    return {
        "user_id": user_id,
        "bucket": target_bucket,
        "prefixes": prefix_list,
        "expires_in": expires_in,
        "count": len(templates),
        "templates": templates,
    }


@ncd_router.get("/template/docx")
async def get_template_docx(
    section: str,
    bucket: Optional[str] = None,
    expires_in: int = 3600,
    aws_region: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return DOCX template download links for a given IND section.
    """
    cleaned = section.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="section is required")
    if "/" in cleaned:
        raise HTTPException(status_code=400, detail="section must not include '/'")
    match = re.match(r"^(\d+\.\d+)", cleaned)
    if not match:
        raise HTTPException(
            status_code=400, detail="section must start with an X.Y pattern"
        )

    target_bucket = bucket or DEFAULT_TEMPLATE_BUCKET
    if not target_bucket:
        raise HTTPException(status_code=400, detail="bucket is required")

    if expires_in <= 0:
        raise HTTPException(status_code=400, detail="expires_in must be positive")

    base_section = match.group(1)
    base_prefix = _normalize_s3_prefix(base_section)

    s3_client = _boto3_client("s3", region_name=aws_region)
    try:
        base_exists = _s3_prefix_exists(s3_client, target_bucket, base_prefix)
    except Exception as exc:  # pragma: no cover - network/auth errors
        _raise_sanitized_http_error(
            status_code=500,
            detail="Failed to check template folder.",
            exc=exc,
            log_message="Failed to check template folder",
        )
    if not base_exists:
        raise HTTPException(
            status_code=404,
            detail=f"Template folder not found for section {base_section}",
        )

    prefixes = [base_prefix]
    if cleaned != base_section:
        sub_prefix = _normalize_s3_prefix(f"{base_section}/{cleaned}")
        prefixes = [sub_prefix, base_prefix]

    prefixes_checked: List[str] = []
    docx_objects: List[Dict[str, Any]] = []
    for prefix in prefixes:
        prefixes_checked.append(prefix)
        try:
            docx_objects = _list_docx_objects(
                s3_client, target_bucket, prefix, direct_only=True
            )
        except Exception as exc:  # pragma: no cover - network/auth errors
            _raise_sanitized_http_error(
                status_code=500,
                detail="Failed to list templates.",
                exc=exc,
                log_message="Failed to list templates",
            )
        if docx_objects:
            break

    templates: List[Dict[str, Any]] = []
    for obj in sorted(docx_objects, key=lambda item: item.get("Key") or ""):
        key = obj.get("Key")
        if not key:
            continue
        try:
            download_url = s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": target_bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except Exception as exc:  # pragma: no cover - network or auth errors
            logger.warning("Failed to presign s3://%s/%s: %s", target_bucket, key, exc)
            download_url = None
        last_modified = obj.get("LastModified")
        templates.append(
            {
                "name": Path(key).name,
                "s3_bucket": target_bucket,
                "s3_key": key,
                "s3_uri": f"s3://{target_bucket}/{key}",
                "download_url": download_url,
                "size_bytes": obj.get("Size"),
                "last_modified": (
                    last_modified.isoformat()
                    if isinstance(last_modified, datetime)
                    else None
                ),
            }
        )

    return {
        "section": cleaned,
        "base_section": base_section,
        "bucket": target_bucket,
        "prefixes_checked": prefixes_checked,
        "expires_in": expires_in,
        "count": len(templates),
        "templates": templates,
    }


@ncd_router.get("/gap-analysis")
async def get_gap_analysis(
    project_id: str,
    bucket: str,
    tenant_id: Optional[str] = None,
    project_prefix: Optional[str] = None,
    include_optional_p1: bool = False,
    aws_region: Optional[str] = None,
) -> Dict[str, Any]:
    """Get gap analysis."""
    _require_uuid(project_id, "project_id")
    if not bucket:
        raise HTTPException(status_code=400, detail="bucket is required")

    def worker() -> Dict[str, Any]:
        """Worker."""
        db = SessionLocal()
        try:
            return _build_gap_analysis_report(
                db,
                project_id=project_id,
                bucket=bucket,
                tenant_id=tenant_id,
                project_prefix=project_prefix,
                include_optional_p1=include_optional_p1,
                aws_region=aws_region,
            )
        finally:
            db.close()

    return await asyncio.to_thread(worker)


@ncd_router.post("/gap-analysis")
async def post_gap_analysis(payload: NCDGapAnalysisRequest) -> Dict[str, Any]:
    """Post gap analysis."""
    _require_uuid(payload.project_id, "project_id")
    if not payload.bucket:
        raise HTTPException(status_code=400, detail="bucket is required")

    def worker() -> Dict[str, Any]:
        """Worker."""
        db = SessionLocal()
        try:
            return _build_gap_analysis_report(
                db,
                project_id=payload.project_id,
                bucket=payload.bucket,
                tenant_id=payload.tenant_id,
                project_prefix=payload.project_prefix,
                include_optional_p1=payload.include_optional_p1,
                aws_region=payload.aws_region,
            )
        finally:
            db.close()

    return await asyncio.to_thread(worker)


@ncd_router.get("/ctd/2.4/element")
async def get_ctd_element_reference(
    element: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Get ctd element reference."""
    normalized = normalize_element_number(element)
    if not normalized:
        raise HTTPException(status_code=400, detail="element is required")
    if not normalized.startswith("2.4."):
        raise HTTPException(status_code=400, detail="element must start with 2.4.")
    if not tenant_id or not project_id or not bucket:
        raise HTTPException(
            status_code=400, detail="tenant_id, project_id, and bucket are required"
        )

    def worker() -> Dict[str, Any]:
        """Worker."""
        db = SessionLocal()
        repo = NCDRepository(session=db)
        try:
            cached = None
            if not refresh:
                cached = repo.fetch_ctd_section_reference(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    bucket=bucket,
                    element_number=normalized,
                )
            if cached and cached.get("payload"):
                payload = cached.get("payload")
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError:
                        payload = {"payload": payload}
                if isinstance(payload, dict):
                    payload["cache"] = {
                        "cached": True,
                        "updated_at": cached.get("updated_at"),
                    }
                return payload

            payload = build_ctd_element_reference(
                db,
                tenant_id=tenant_id,
                project_id=project_id,
                bucket=bucket,
                element_number=normalized,
            )
            try:
                repo.upsert_ctd_section_reference(
                    record=CTDSectionReferenceRecord(
                        tenant_id=tenant_id,
                        project_id=project_id,
                        bucket=bucket,
                        element_number=normalized,
                        section_number=payload.get("section_number"),
                        template_payload=payload.get("template") or {},
                        module4_sections=payload.get("module4_sections") or [],
                        payload=payload,
                    )
                )
            except (
                Exception
            ) as exc:  # pragma: no cover - cache failure should not block response
                logger.warning("Failed to cache CTD element reference: %s", exc)
            payload["cache"] = {"cached": False, "updated_at": None}
            return payload
        finally:
            db.close()

    return await asyncio.to_thread(worker)


@ncd_router.get("/ctd/2.6/section")
async def get_ctd_section_materials(
    section: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
    include_tables: bool = True,
    include_images: bool = True,
) -> Dict[str, Any]:
    """
    Return Module 4 material and NCD data mapped to a single CTD 2.6 section.
    """
    target = section.strip()
    if not target:
        raise HTTPException(status_code=400, detail="section is required")
    if not target.startswith("2.6."):
        raise HTTPException(status_code=400, detail="section must start with 2.6.")
    if not tenant_id or not project_id or not bucket:
        raise HTTPException(
            status_code=400, detail="tenant_id, project_id, and bucket are required"
        )

    db = SessionLocal()
    try:
        project_name = fetch_project_name(db, project_id)
        project_like = f"%/{project_name}/%" if project_name else "%"

        mapping_entries = module4_sections_for_ctd(target)
        filtered_mappings: List[Dict[str, Any]] = []
        for entry in mapping_entries:
            matched_targets = [
                target_entry
                for target_entry in entry.get("targets", [])
                if target_entry.get("section") == target
            ]
            if not matched_targets:
                continue
            filtered = dict(entry)
            filtered["matched_targets"] = matched_targets
            filtered_mappings.append(filtered)

        module4_sections = [entry["module4_section"] for entry in filtered_mappings]

        sources = fetch_section_sources(
            db,
            tenant_id=tenant_id,
            bucket=bucket,
            project_like=project_like,
            module4_sections=module4_sections,
        )
        ncd_payload = fetch_ncd_payload(
            db,
            project_id=project_id,
            module4_sections=module4_sections,
        )
    finally:
        db.close()

    s3_client = _boto3_client("s3")
    markdown_cache: Dict[str, Optional[str]] = {}
    response_sources: List[Dict[str, Any]] = []

    for row in sources:
        section_number = str(row.get("section_number") or "")
        matched_module4 = [
            module4
            for module4 in module4_sections
            if section_number_matches(section_number, module4)
        ]
        summary_text = row.get("summary_text")
        if isinstance(summary_text, str) and summary_text.strip():
            try:
                summary_text = json.loads(summary_text)
            except json.JSONDecodeError:
                summary_text = {"summary": summary_text}

        markdown_key = f"{row['s3_key']}.extracted.md"
        markdown_s3_uri = f"s3://{row['s3_bucket']}/{markdown_key}"
        pdf_s3_uri = f"s3://{row['s3_bucket']}/{row['s3_key']}"

        tables_html: List[str] = []
        images: List[str] = []
        if include_tables or include_images:
            markdown = markdown_cache.get(markdown_key)
            if markdown is None:
                markdown = _read_s3_text(s3_client, row["s3_bucket"], markdown_key)
                markdown_cache[markdown_key] = markdown
            if markdown:
                slice_text = markdown_slice(
                    markdown,
                    int(row.get("char_start") or 0),
                    int(row.get("char_end") or 0),
                )
                if include_tables:
                    tables_html = extract_markdown_tables(slice_text)
                if include_images:
                    images = extract_markdown_images(
                        slice_text, row["s3_bucket"], row["s3_key"]
                    )

        response_sources.append(
            {
                "module4_sections": matched_module4,
                "section_number": section_number,
                "section_title": row.get("section_title"),
                "summary": summary_text,
                "keywords": row.get("keywords") or [],
                "summary_type": row.get("summary_type"),
                "summary_purpose": row.get("summary_purpose"),
                "document_section_id": row.get("section_id"),
                "document_version_id": row.get("document_version_id"),
                "document_id": row.get("document_id"),
                "slice": {
                    "char_start": row.get("char_start"),
                    "char_end": row.get("char_end"),
                    "page_start": row.get("page_start"),
                    "page_end": row.get("page_end"),
                },
                "s3": {
                    "bucket": row.get("s3_bucket"),
                    "key": row.get("s3_key"),
                    "version_id": row.get("s3_version_id"),
                    "pdf_s3_uri": pdf_s3_uri,
                    "markdown_s3_uri": markdown_s3_uri,
                },
                "tables_html": tables_html,
                "images": images,
            }
        )

    return {
        "section": target,
        "project_id": project_id,
        "tenant_id": tenant_id,
        "bucket": bucket,
        "project_name": project_name,
        "mapping": filtered_mappings,
        "sources": response_sources,
        "ncd": ncd_payload,
    }


@ncd_router.get("/assets/section")
async def get_assets_section(
    section: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
    content_type: Optional[str] = None,
    include_assets: bool = True,
    limit_topics_per_doc: int = 0,
    expires_in: int = 3600,
    aws_region: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Unified context for a CTD 2.4/2.6 section:
    - Groups text, images, and tables by document, then by topic (key section).
    - Each topic carries a short title, description (text), and supporting assets.
    """
    target = section.strip()
    if not target:
        raise HTTPException(status_code=400, detail="section is required")
    if not (target.startswith("2.4") or target.startswith("2.6")):
        raise HTTPException(
            status_code=400, detail="section must start with 2.4 or 2.6"
        )
    if content_type and content_type not in {"summary", "conclusion"}:
        raise HTTPException(
            status_code=400, detail="content_type must be summary or conclusion"
        )
    if not tenant_id or not project_id or not bucket:
        raise HTTPException(
            status_code=400, detail="tenant_id, project_id, and bucket are required"
        )
    if expires_in <= 0:
        raise HTTPException(status_code=400, detail="expires_in must be positive")

    db = SessionLocal()
    try:
        project_name = fetch_project_name(db, project_id)
        project_like = f"%/{project_name}/%" if project_name else "%"

        module4_sections, mapping_entries, targets = module4_sections_for_ctd_targets(
            target
        )
        if target.startswith("2.6."):
            exact_sections = set()
            filtered_mappings: List[Dict[str, Any]] = []
            for entry in mapping_entries:
                matched_targets = [
                    t for t in entry.get("targets", []) if t.get("section") == target
                ]
                if matched_targets:
                    filtered = dict(entry)
                    filtered["matched_targets"] = matched_targets
                    filtered_mappings.append(filtered)
                    exact_sections.add(entry.get("module4_section"))
            if filtered_mappings:
                module4_sections = sorted(s for s in exact_sections if s)
                mapping_entries = filtered_mappings

        key_sections = fetch_key_sections_for_sections(
            db,
            tenant_id=tenant_id,
            bucket=bucket,
            project_like=project_like,
            module4_sections=module4_sections,
            section_type=content_type,
        )
        assets: List[Dict[str, Any]] = []
        if include_assets:
            assets = fetch_assets_for_sections(
                db,
                tenant_id=tenant_id,
                bucket=bucket,
                project_like=project_like,
                module4_sections=module4_sections,
                asset_type=None,
            )
            # Best-effort presign for UI download
            if boto3:
                try:
                    s3_client = _boto3_client("s3", region_name=aws_region)
                    # enrich table assets with previews/columns if available
                    for asset in assets:
                        if asset.get("asset_type") != "table":
                            continue
                        extra = _normalize_extra_attributes(asset.get("extra_attributes"))
                        json_key = extra.get("json_key")
                        if json_key:
                            preview_rows = _read_table_json_preview(
                                s3_client,
                                bucket=asset.get("s3_bucket") or bucket,
                                key=json_key,
                                max_rows=20,
                            )
                            if preview_rows:
                                asset["preview_rows"] = preview_rows
                        asset["columns"] = extra.get("columns") or asset.get("columns") or []
                        asset["json_key"] = json_key
                        asset["row_count"] = extra.get("row_count")
                    for asset in assets:
                        key = asset.get("s3_key")
                        bkt = asset.get("s3_bucket") or bucket
                        if not key or not bkt:
                            continue
                        try:
                            asset["download_url"] = s3_client.generate_presigned_url(
                                "get_object",
                                Params={"Bucket": bkt, "Key": key},
                                ExpiresIn=expires_in,
                            )
                        except Exception:
                            asset["download_url"] = None
                except Exception:
                    # leave download_url absent on presign failure
                    pass
            # For /assets/section, expose download URLs for images instead of s3_key.
            for asset in assets:
                if asset.get("asset_type") == "table":
                    continue
                if "download_url" not in asset:
                    asset["download_url"] = None
                asset.pop("s3_key", None)
    finally:
        db.close()

    # group by document_version_id
    documents: Dict[str, Dict[str, Any]] = {}
    for ks in key_sections:
        doc_vid = str(ks.get("document_version_id"))
        if not doc_vid:
            continue
        doc = documents.setdefault(
            doc_vid,
            {
                "document_version_id": doc_vid,
                "document_s3_key": ks.get("document_s3_key"),
                "document_name": Path(ks.get("document_s3_key") or "").name
                if ks.get("document_s3_key")
                else None,
                "s3_bucket": ks.get("s3_bucket"),
                "topics": [],
            },
        )
        if limit_topics_per_doc and len(doc["topics"]) >= limit_topics_per_doc:
            continue
        topic_id = str(ks.get("id"))
        topic_assets = (
            _attach_assets_to_topic(
                assets=assets,
                document_version_id=doc_vid,
                page_start=ks.get("page_start"),
                page_end=ks.get("page_end"),
            )
            if include_assets
            else {"images": [], "tables": []}
        )
        # render HTML for tables so UI can drop into editors easily
        for tbl in topic_assets["tables"]:
            if "html" not in tbl:
                tbl["html"] = _render_table_html(
                    tbl.get("columns") or [],
                    tbl.get("preview_rows") or [],
                    caption=tbl.get("caption"),
                )
        doc["topics"].append(
            {
                "topic_id": topic_id,
                "title": _build_topic_title(ks.get("text") or ""),
                "description": _truncate_text(ks.get("text"), 800),
                "section_type": ks.get("section_type"),
                "page_start": ks.get("page_start"),
                "page_end": ks.get("page_end"),
                "char_start": ks.get("char_start"),
                "char_end": ks.get("char_end"),
                "assets": {
                    "images": topic_assets["images"],
                    "tables": topic_assets["tables"],
                },
            }
        )

    # If no key sections but assets exist, still surface them per document.
    if include_assets and assets and not documents:
        by_doc = defaultdict(list)
        for asset in assets:
            vid = str(asset.get("document_version_id"))
            by_doc[vid].append(asset)
        for vid, items in by_doc.items():
            documents[vid] = {
                "document_version_id": vid,
                "document_s3_key": items[0].get("document_s3_key"),
                "s3_bucket": items[0].get("s3_bucket"),
                "topics": [
                    {
                        "topic_id": f"{vid}-assets",
                        "title": "Assets",
                        "description": "",
                        "section_type": None,
                        "page_start": None,
                        "page_end": None,
                        "char_start": None,
                        "char_end": None,
                        "assets": {
                            "images": [
                                a for a in items if a.get("asset_type") != "table"
                            ],
                            "tables": [a for a in items if a.get("asset_type") == "table"],
                        },
                    }
                ],
            }

    return {
        "ctd_section": target,
        "ctd_targets": targets,
        "module4_sections": module4_sections,
        "mapping": mapping_entries,
        "documents": list(documents.values()),
    }


@ncd_router.get("/assets/image")
async def get_assets_images(
    section: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
    limit: int = 500,
) -> Dict[str, Any]:
    """Get assets images."""
    return await _get_assets_by_type(
        section=section,
        tenant_id=tenant_id,
        project_id=project_id,
        bucket=bucket,
        asset_type="image",
        limit=limit,
    )


@ncd_router.get("/assets/table")
async def get_assets_tables(
    section: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
    limit: int = 500,
) -> Dict[str, Any]:
    """Get assets tables."""
    return await _get_assets_by_type(
        section=section,
        tenant_id=tenant_id,
        project_id=project_id,
        bucket=bucket,
        asset_type="table",
        limit=limit,
    )


async def _get_assets_by_type(
    *,
    section: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
    asset_type: str,
    limit: int,
) -> Dict[str, Any]:
    """Return assets for a CTD section filtered by type."""
    target = section.strip()
    if not target:
        raise HTTPException(status_code=400, detail="section is required")
    if not (target.startswith("2.4") or target.startswith("2.6")):
        raise HTTPException(
            status_code=400, detail="section must start with 2.4 or 2.6"
        )
    if asset_type not in {"image", "table"}:
        raise HTTPException(status_code=400, detail="asset_type must be image or table")
    if not tenant_id or not project_id or not bucket:
        raise HTTPException(
            status_code=400, detail="tenant_id, project_id, and bucket are required"
        )

    db = SessionLocal()
    try:
        project_name = fetch_project_name(db, project_id)
        project_like = f"%/{project_name}/%" if project_name else "%"

        module4_sections, mapping_entries, targets = module4_sections_for_ctd_targets(
            target
        )
        if target.startswith("2.6."):
            exact_sections = set()
            filtered_mappings: List[Dict[str, Any]] = []
            for entry in mapping_entries:
                matched_targets = [
                    t for t in entry.get("targets", []) if t.get("section") == target
                ]
                if matched_targets:
                    filtered = dict(entry)
                    filtered["matched_targets"] = matched_targets
                    filtered_mappings.append(filtered)
                    exact_sections.add(entry.get("module4_section"))
            if filtered_mappings:
                module4_sections = sorted(s for s in exact_sections if s)
                mapping_entries = filtered_mappings

        assets = fetch_assets_for_sections(
            db,
            tenant_id=tenant_id,
            bucket=bucket,
            project_like=project_like,
            module4_sections=module4_sections,
            asset_type=asset_type,
        )
    finally:
        db.close()

    payload: List[Dict[str, Any]] = []
    for row in assets[: max(0, limit)]:
        payload.append(
            {
                "id": row.get("id"),
                "asset_type": row.get("asset_type"),
                "page_number": row.get("page_number"),
                "index_on_page": row.get("index_on_page"),
                "s3_bucket": row.get("s3_bucket"),
                "s3_key": row.get("s3_key"),
                "caption": row.get("caption"),
                "description": row.get("description"),
                "keywords": row.get("keywords") or [],
                "extra_attributes": _normalize_extra_attributes(
                    row.get("extra_attributes")
                ),
                "document_version_id": row.get("document_version_id"),
                "document_s3_key": row.get("document_s3_key"),
            }
        )

    return {
        "ctd_section": target,
        "ctd_targets": targets,
        "module4_sections": module4_sections,
        "mapping": mapping_entries,
        "assets": payload,
    }


@ncd_router.get("/assets/contents")
async def get_assets_contents(
    section: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
    content_type: Optional[str] = None,
    include_assets: bool = True,
) -> Dict[str, Any]:
    """Get assets contents."""
    target = section.strip()
    if not target:
        raise HTTPException(status_code=400, detail="section is required")
    if not (target.startswith("2.4") or target.startswith("2.6")):
        raise HTTPException(
            status_code=400, detail="section must start with 2.4 or 2.6"
        )
    if content_type and content_type not in {"summary", "conclusion"}:
        raise HTTPException(
            status_code=400, detail="content_type must be summary or conclusion"
        )
    if not tenant_id or not project_id or not bucket:
        raise HTTPException(
            status_code=400, detail="tenant_id, project_id, and bucket are required"
        )

    db = SessionLocal()
    try:
        project_name = fetch_project_name(db, project_id)
        project_like = f"%/{project_name}/%" if project_name else "%"

        module4_sections, mapping_entries, targets = module4_sections_for_ctd_targets(
            target
        )
        if target.startswith("2.6."):
            exact_sections = set()
            filtered_mappings: List[Dict[str, Any]] = []
            for entry in mapping_entries:
                matched_targets = [
                    t for t in entry.get("targets", []) if t.get("section") == target
                ]
                if matched_targets:
                    filtered = dict(entry)
                    filtered["matched_targets"] = matched_targets
                    filtered_mappings.append(filtered)
                    exact_sections.add(entry.get("module4_section"))
            if filtered_mappings:
                module4_sections = sorted(s for s in exact_sections if s)
                mapping_entries = filtered_mappings

        key_sections = fetch_key_sections_for_sections(
            db,
            tenant_id=tenant_id,
            bucket=bucket,
            project_like=project_like,
            module4_sections=module4_sections,
            section_type=content_type,
        )
        assets_by_id: Dict[str, Dict[str, Any]] = {}
        if include_assets:
            asset_ids: List[str] = []
            for row in key_sections:
                asset_ids.extend(_normalize_uuid_list(row.get("asset_ids") or []))
            if asset_ids:
                assets = (
                    db.execute(
                        sqltext(
                            """
                            SELECT id, asset_type, page_number, index_on_page,
                                   s3_bucket, s3_key, caption, description, keywords,
                                   extra_attributes, document_version_id
                            FROM document_assets
                            WHERE id = ANY(CAST(:ids AS uuid[]))
                            """
                        ),
                        {"ids": asset_ids},
                    )
                    .mappings()
                    .all()
                )
                for asset in assets:
                    payload = dict(asset)
                    payload["extra_attributes"] = _normalize_extra_attributes(
                        payload.get("extra_attributes")
                    )
                    assets_by_id[str(payload["id"])] = payload
    finally:
        db.close()

    contents: List[Dict[str, Any]] = []
    for row in key_sections:
        asset_list: List[Dict[str, Any]] = []
        if include_assets:
            for asset_id in row.get("asset_ids") or []:
                payload = assets_by_id.get(str(asset_id))
                if payload:
                    asset_list.append(payload)
        contents.append(
            {
                "id": row.get("id"),
                "section_type": row.get("section_type"),
                "text": row.get("text"),
                "page_start": row.get("page_start"),
                "page_end": row.get("page_end"),
                "char_start": row.get("char_start"),
                "char_end": row.get("char_end"),
                "asset_ids": row.get("asset_ids") or [],
                "assets": asset_list,
                "model_name": row.get("model_name"),
                "confidence": row.get("confidence"),
                "document_version_id": row.get("document_version_id"),
                "s3_bucket": row.get("s3_bucket"),
                "document_s3_key": row.get("document_s3_key"),
            }
        )

    return {
        "ctd_section": target,
        "ctd_targets": targets,
        "module4_sections": module4_sections,
        "mapping": mapping_entries,
        "contents": contents,
    }


@ncd_router.post("/assets/summary")
async def create_ctd_section_summary(
    payload: CTDSectionSummaryRequest,
    request: Request,
) -> Dict[str, Any]:
    """Create ctd section summary."""
    _enforce_llm_endpoint_rate_limit(request, endpoint="summary")
    section = payload.section.strip()
    if not section:
        raise HTTPException(status_code=400, detail="section is required")
    if not (section.startswith("2.4") or section.startswith("2.6")):
        raise HTTPException(
            status_code=400, detail="section must start with 2.4 or 2.6"
        )
    _require_uuid(payload.tenant_id, "tenant_id")
    _require_uuid(payload.project_id, "project_id")
    if not payload.bucket:
        raise HTTPException(status_code=400, detail="bucket is required")
    if payload.refresh_template:
        _reset_ind_template_cache()
    normalized_user_prompt = _normalize_user_prompt(payload.user_prompt)

    def worker() -> Dict[str, Any]:
        """Worker."""
        db = SessionLocal()
        repo = NCDRepository(session=db)
        try:
            context, element_numbers = _build_section_summary_context(
                db,
                section=section,
                tenant_id=payload.tenant_id,
                project_id=payload.project_id,
                bucket=payload.bucket,
            )
            has_context = bool(
                context.get("elements")
                or context.get("section_sources")
                or context.get("section_key_sections")
                or context.get("template_entries")
                or context.get("table_assets")
            )
            if not has_context:
                raise HTTPException(
                    status_code=404, detail="No data found for the requested section"
                )

            previous_summary_id = _normalize_optional_uuid(
                payload.previous_summary_id,
                "previous_summary_id",
            )
            previous_summary: Optional[str] = None
            previous_embedding: Any = None
            if payload.user_comment:
                if previous_summary_id:
                    previous = repo.fetch_ctd_section_summary(
                        summary_id=previous_summary_id
                    )
                else:
                    previous = repo.fetch_latest_ctd_section_summary(
                        tenant_id=payload.tenant_id,
                        project_id=payload.project_id,
                        bucket=payload.bucket,
                        section_number=section,
                    )
                if not previous:
                    raise HTTPException(
                        status_code=404,
                        detail="Previous summary not found for user_comment",
                    )
                if (
                    str(previous.get("tenant_id")) != payload.tenant_id
                    or str(previous.get("project_id")) != payload.project_id
                    or str(previous.get("bucket")) != payload.bucket
                ):
                    raise HTTPException(
                        status_code=404, detail="Previous summary not found"
                    )
                previous_summary_id = str(previous.get("id"))
                previous_summary = previous.get("final_text") or previous.get(
                    "summary_text"
                )
                previous_embedding = previous.get("embedding")

            system_prompt, llm_user_prompt = _build_section_summary_prompt(
                section=section,
                context=context,
                user_prompt=normalized_user_prompt,
                user_comment=payload.user_comment,
                previous_summary=previous_summary,
                previous_embedding=previous_embedding,
            )
            llm = LLMClient()
            try:
                summary_text = llm.generate_text(system_prompt, llm_user_prompt).strip()
            except Exception as exc:
                _raise_sanitized_http_error(
                    status_code=500,
                    detail="Failed to generate summary.",
                    exc=exc,
                    log_message="Failed to generate section summary",
                )
            if not summary_text:
                raise HTTPException(
                    status_code=500, detail="Summary generation returned empty output"
                )

            embedding = _generate_embedding(summary_text)
            try:
                summary_id = repo.insert_ctd_section_summary(
                    record=CTDSectionSummaryRecord(
                        tenant_id=payload.tenant_id,
                        project_id=payload.project_id,
                        bucket=payload.bucket,
                        section_number=section,
                        summary_text=summary_text,
                        status="draft",
                        element_numbers=element_numbers,
                        user_prompt=normalized_user_prompt,
                        user_comment=payload.user_comment,
                        previous_id=previous_summary_id,
                        model_name=llm.model_name,
                        embedding=embedding,
                    )
                )
            except Exception as exc:
                _raise_sanitized_http_error(
                    status_code=500,
                    detail="Failed to store section summary.",
                    exc=exc,
                    log_message="Failed to store section summary",
                )
        finally:
            db.close()

        return {
            "summary_id": summary_id,
            "section": section,
            "status": "draft",
            "summary_text": summary_text,
            "element_numbers": element_numbers,
            "previous_summary_id": previous_summary_id,
        }

    return await asyncio.to_thread(worker)


@ncd_router.post("/assets/summary/approve")
async def approve_ctd_section_summary(
    payload: CTDSectionSummaryApproveRequest,
    request: Request,
) -> Dict[str, Any]:
    """Approve ctd section summary."""
    _enforce_llm_endpoint_rate_limit(request, endpoint="summary_approve")
    if not payload.final_text or not payload.final_text.strip():
        raise HTTPException(status_code=400, detail="final_text is required")
    _require_uuid(payload.tenant_id, "tenant_id")
    _require_uuid(payload.project_id, "project_id")
    if not payload.bucket:
        raise HTTPException(status_code=400, detail="bucket is required")

    summary_id = _normalize_optional_uuid(payload.summary_id, "summary_id")
    section = payload.section.strip() if payload.section else ""

    db = SessionLocal()
    repo = NCDRepository(session=db)
    try:
        if summary_id:
            existing = repo.fetch_ctd_section_summary(summary_id=summary_id)
        else:
            if not section:
                raise HTTPException(
                    status_code=400,
                    detail="summary_id or section is required",
                )
            existing = repo.fetch_latest_ctd_section_summary(
                tenant_id=payload.tenant_id,
                project_id=payload.project_id,
                bucket=payload.bucket,
                section_number=section,
                status="draft",
            )
            summary_id = str(existing.get("id")) if existing else None

        if not existing or not summary_id:
            raise HTTPException(status_code=404, detail="Summary not found")
        if (
            str(existing.get("tenant_id")) != payload.tenant_id
            or str(existing.get("project_id")) != payload.project_id
            or str(existing.get("bucket")) != payload.bucket
        ):
            raise HTTPException(status_code=404, detail="Summary not found")

        updated = repo.approve_ctd_section_summary(
            summary_id=summary_id,
            final_text=payload.final_text.strip(),
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Summary not found")
    finally:
        db.close()

    return {
        "summary_id": summary_id,
        "section": updated.get("section_number"),
        "status": updated.get("status"),
        "summary_text": updated.get("summary_text"),
        "final_text": updated.get("final_text"),
    }


@ncd_router.post("/assets/tabulated")
async def create_ctd_tabulated_summary(
    payload: CTDTabulatedSummaryRequest,
    request: Request,
) -> Dict[str, Any]:
    """Create ctd tabulated summary."""
    _enforce_llm_endpoint_rate_limit(request, endpoint="tabulated")
    section = payload.section.strip()
    if not section:
        raise HTTPException(status_code=400, detail="section is required")
    if not section.startswith("2.6"):
        raise HTTPException(status_code=400, detail="section must start with 2.6")
    _require_uuid(payload.tenant_id, "tenant_id")
    _require_uuid(payload.project_id, "project_id")
    if not payload.bucket:
        raise HTTPException(status_code=400, detail="bucket is required")
    if payload.refresh_template:
        _reset_ind_template_cache()
    normalized_user_prompt = _normalize_user_prompt(payload.user_prompt)

    db = SessionLocal()
    repo = NCDRepository(session=db)
    try:
        use_llm = payload.use_llm if payload.use_llm is not None else True
        context, debug_info = _build_tabulated_context(
            db,
            section=section,
            tenant_id=payload.tenant_id,
            project_id=payload.project_id,
            bucket=payload.bucket,
        )
        table_specs = context.get("table_specs") or []
        if not table_specs:
            raise HTTPException(
                status_code=404, detail="No tabulated template entries found"
            )

        previous_tabulated_id = _normalize_optional_uuid(
            payload.previous_tabulated_id,
            "previous_tabulated_id",
        )
        previous_tables: Optional[Dict[str, Any]] = None
        previous_embedding: Any = None
        if payload.user_comment or previous_tabulated_id:
            if previous_tabulated_id:
                previous = repo.fetch_ctd_tabulated_summary(
                    summary_id=previous_tabulated_id
                )
            else:
                previous = repo.fetch_latest_ctd_tabulated_summary(
                    tenant_id=payload.tenant_id,
                    project_id=payload.project_id,
                    bucket=payload.bucket,
                    section_number=section,
                )
            if not previous:
                detail = (
                    "Previous tabulated summary not found for user_comment"
                    if payload.user_comment
                    else "Previous tabulated summary not found"
                )
                raise HTTPException(status_code=404, detail=detail)
            if (
                str(previous.get("tenant_id")) != payload.tenant_id
                or str(previous.get("project_id")) != payload.project_id
                or str(previous.get("bucket")) != payload.bucket
            ):
                raise HTTPException(
                    status_code=404, detail="Previous tabulated summary not found"
                )
            previous_tabulated_id = str(previous.get("id"))
            previous_tables = _normalize_json_dict(previous.get("table_payload"))
            previous_embedding = previous.get("embedding")

        llm_model_name: Optional[str] = None
        if not use_llm:
            tables = []
            if previous_tables:
                tables = (
                    previous_tables.get("tables")
                    if isinstance(previous_tables, dict)
                    else []
                )
            if not isinstance(tables, list):
                tables = []
            _realign_tabulated_tables_by_columns(table_specs, tables)
            merged_tables = _merge_tabulated_tables(table_specs, tables)
            for spec, table in zip(table_specs, merged_tables):
                if table.get("notes"):
                    continue
                notes: List[str] = []
                description = spec.get("table_description")
                row_content = spec.get("row_content")
                if description:
                    notes.append(str(description))
                if row_content:
                    notes.append(str(row_content))
                if normalized_user_prompt:
                    notes.append(f"User prompt: {normalized_user_prompt}")
                if payload.user_comment:
                    notes.append(f"User comment: {payload.user_comment}")
                if notes:
                    table["notes"] = " ".join(
                        note.strip() for note in notes if note.strip()
                    )
            llm_model_name = "template-only"
            _normalize_tabulated_study_ids(merged_tables, context)
            _repair_overview_table(merged_tables, context)
            _repair_primary_pharmacodynamics_table(merged_tables, context)
            _repair_safety_pharmacology_table(merged_tables, context)
        else:
            system_prompt, llm_user_prompt = _build_tabulated_prompt(
                section=section,
                context=context,
                user_prompt=normalized_user_prompt,
                user_comment=payload.user_comment,
                previous_tables=previous_tables,
                previous_embedding=previous_embedding,
            )
            llm = LLMClient()
            llm_model_name = llm.model_name
            try:
                response = llm.extract_json(system_prompt, llm_user_prompt)
            except Exception as exc:
                _raise_sanitized_http_error(
                    status_code=500,
                    detail="Failed to generate tabulated summary.",
                    exc=exc,
                    log_message="Failed to generate tabulated summary",
                )

            tables = response.get("tables") if isinstance(response, dict) else []
            if not isinstance(tables, list):
                tables = []
            _realign_tabulated_tables_by_columns(table_specs, tables)
            merged_tables = _merge_tabulated_tables(table_specs, tables)
            _normalize_tabulated_study_ids(merged_tables, context)
            _repair_overview_table(merged_tables, context)
            _repair_primary_pharmacodynamics_table(merged_tables, context)
            _repair_safety_pharmacology_table(merged_tables, context)
        table_payload = {
            "section": section,
            "tables": merged_tables,
        }

        embedding = _generate_embedding(json.dumps(table_payload, ensure_ascii=True))
        try:
            summary_id = repo.insert_ctd_tabulated_summary(
                record=CTDTabulatedSummaryRecord(
                    tenant_id=payload.tenant_id,
                    project_id=payload.project_id,
                    bucket=payload.bucket,
                    section_number=section,
                    table_payload=table_payload,
                    status="draft",
                    user_prompt=normalized_user_prompt,
                    user_comment=payload.user_comment,
                    previous_id=previous_tabulated_id,
                    model_name=llm_model_name,
                    embedding=embedding,
                )
            )
        except Exception as exc:
            _raise_sanitized_http_error(
                status_code=500,
                detail="Failed to store tabulated summary.",
                exc=exc,
                log_message="Failed to store tabulated summary",
            )
    finally:
        db.close()

    return {
        "tabulated_id": summary_id,
        "section": section,
        "status": "draft",
        "tables": merged_tables,
        "context": {
            "table_specs": table_specs,
            "table_assets": context.get("table_assets") or [],
            "section_sources": context.get("section_sources") or [],
            "module4_sections": context.get("module4_sections") or [],
            "mapping": context.get("mapping") or [],
            "ctd_targets": context.get("ctd_targets") or [],
        },
        "debug": debug_info,
        "previous_tabulated_id": previous_tabulated_id,
    }


@ncd_router.post("/assets/tabulated/approve")
async def approve_ctd_tabulated_summary(
    payload: CTDTabulatedSummaryApproveRequest,
) -> Dict[str, Any]:
    """Approve ctd tabulated summary."""
    if not payload.final_payload:
        raise HTTPException(status_code=400, detail="final_payload is required")
    _require_uuid(payload.tenant_id, "tenant_id")
    _require_uuid(payload.project_id, "project_id")
    if not payload.bucket:
        raise HTTPException(status_code=400, detail="bucket is required")

    summary_id = _normalize_optional_uuid(payload.summary_id, "summary_id")
    section = payload.section.strip() if payload.section else ""

    db = SessionLocal()
    repo = NCDRepository(session=db)
    try:
        if summary_id:
            existing = repo.fetch_ctd_tabulated_summary(summary_id=summary_id)
        else:
            if not section:
                raise HTTPException(
                    status_code=400,
                    detail="summary_id or section is required",
                )
            existing = repo.fetch_latest_ctd_tabulated_summary(
                tenant_id=payload.tenant_id,
                project_id=payload.project_id,
                bucket=payload.bucket,
                section_number=section,
                status="draft",
            )
            summary_id = str(existing.get("id")) if existing else None

        if not existing or not summary_id:
            raise HTTPException(status_code=404, detail="Tabulated summary not found")
        if (
            str(existing.get("tenant_id")) != payload.tenant_id
            or str(existing.get("project_id")) != payload.project_id
            or str(existing.get("bucket")) != payload.bucket
        ):
            raise HTTPException(status_code=404, detail="Tabulated summary not found")

        updated = repo.approve_ctd_tabulated_summary(
            summary_id=summary_id,
            final_payload=payload.final_payload,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Tabulated summary not found")
    finally:
        db.close()

    return {
        "tabulated_id": summary_id,
        "section": updated.get("section_number"),
        "status": updated.get("status"),
        "table_payload": updated.get("table_payload"),
        "final_payload": updated.get("final_payload"),
    }
