# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""FastAPI service that powers PDF analysis, uploads, and downstream tooling."""

# @author: Bin Lee
# @email: blee@filynai.com

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Sequence, Tuple, cast
from urllib.parse import urlencode
import uuid

try:
    import boto3
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    boto3 = None  # type: ignore[assignment]
try:
    from botocore.exceptions import ClientError
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    ClientError = Exception  # type: ignore[assignment]
import pandas as pd
from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from rapidfuzz import fuzz
from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

from pdf_analysis.api.constants import (
    APP_DESCRIPTION,
    APP_TITLE,
    APP_VERSION,
    CORS_ALLOW_CREDENTIALS,
    CORS_ALLOW_HEADERS,
    CORS_ALLOW_METHODS,
    CORS_ALLOW_ORIGIN_REGEX,
    CORS_ALLOW_ORIGINS,
    DEFAULT_TEMPLATE_BUCKET,
    DEFAULT_TEMPLATE_PREFIXES,
    DEV_ROUTER_PREFIX,
    DEV_ROUTER_TAGS,
    NCD_ROUTER_PREFIX,
    NCD_ROUTER_TAGS,
    SECTION_PROMPT_MAX_CHARS,
    STUDY_ID_EXT_RE,
    STUDY_ID_PREFIX_RE,
    STUDY_ID_RE,
    STUDY_ID_SKIP_RE,
    STUDY_ID_TRAILERS,
    UPLOAD_ROUTER_TAGS,
)
from pdf_analysis.ingest.pdf_text import extract_pages_text
from pdf_analysis.ingest.tables import extract_tables_all
from pdf_analysis.pipeline import PDFProcessingPipeline
from pdf_analysis.service.ai_metadata import (
    OpenAIMetadataGenerator,
    _extract_text_from_response,
)
from pdf_analysis.service.document_summarizer import (
    OpenAIDocumentSummarizer,
    embed_topics_into_markdown,
    format_summary_text,
)

try:  # Optional OpenAI dependency for section summary embeddings.
    from openai import OpenAI
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore[misc]

try:
    from pdf_analysis.service.embedding_store import SupabaseEmbeddingStore
except Exception:  # pragma: no cover - optional dependency or missing extras
    SupabaseEmbeddingStore = None  # type: ignore[misc,assignment]
from pdf_analysis.transform.markdown_writer import (
    build_html_document,
    build_markdown_document,
)
from pdf_analysis.validate import generate_quality_report
from ncd.types.ctd_elements import build_ctd_element_reference
from ncd.types.ctd_materials import (
    extract_markdown_images,
    extract_markdown_tables,
    fetch_assets_for_sections,
    fetch_document_keys_for_sections,
    fetch_key_sections_for_sections,
    fetch_ncd_payload,
    fetch_project_document_keys,
    fetch_project_name,
    fetch_section_sources,
    fetch_study_ids_for_sections,
    markdown_slice,
    module4_sections_for_ctd,
    module4_sections_for_ctd_targets,
    section_number_matches,
)
from ncd.config.ctd_template import (
    load_template_entries,
    normalize_element_number,
    resolve_template_path,
)
from ncd.database.db import SessionLocal
from ncd.config.config import settings
from database.db_interface import (
    CTDSectionReferenceRecord,
    CTDSectionSummaryRecord,
    CTDTabulatedSummaryRecord,
    NCDRepository,
)
from ncd.llm.llm_client import LLMClient

app = FastAPI(
    title=APP_TITLE,
    description=APP_DESCRIPTION,
    version=APP_VERSION,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_origin_regex=CORS_ALLOW_ORIGIN_REGEX,
    allow_credentials=CORS_ALLOW_CREDENTIALS,
    allow_methods=CORS_ALLOW_METHODS,
    allow_headers=CORS_ALLOW_HEADERS,
)


@app.get("/health", tags=["health"])
def health_check() -> Dict[str, str]:
    return {"status": "ok"}


upload_router = APIRouter(tags=UPLOAD_ROUTER_TAGS)
ncd_router = APIRouter(prefix=NCD_ROUTER_PREFIX, tags=NCD_ROUTER_TAGS)
dev_router = APIRouter(prefix=DEV_ROUTER_PREFIX, tags=DEV_ROUTER_TAGS)

logger = logging.getLogger(__name__)


def _table_to_payload(
    table: Dict[str, Any], *, max_rows: Optional[int] = None
) -> Dict[str, Any]:
    """Transform a dataframe-backed table entry into a JSON-safe payload."""
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
    """Build lightweight manifest entries used by markdown rendering."""
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


class NCDLabelRequest(BaseModel):
    """Request payload for minimal section labeling of a PDF stored in S3."""

    key: str
    company: str
    project: str
    bucket: Optional[str] = None
    aws_region: Optional[str] = None
    page_limit: int = 5
    use_llm: bool = False


class NCDRelabelRequest(BaseModel):
    """Request payload to correct a labeled section for a PDF in S3."""

    key: str
    company: str
    project: str
    section_number: str
    section_title: Optional[str] = None
    bucket: Optional[str] = None
    aws_region: Optional[str] = None


class TemplateOverrideRequest(BaseModel):
    """Create or update a template override for a user."""

    user_id: str
    section: str
    subsection: Optional[str] = None
    payload: Dict[str, Any]
    aws_region: Optional[str] = None


class CTDSectionSummaryRequest(BaseModel):
    """Request payload for generating a CTD section summary draft."""

    section: str
    tenant_id: str
    project_id: str
    bucket: str
    user_prompt: Optional[str] = None
    user_comment: Optional[str] = None
    previous_summary_id: Optional[str] = None
    refresh_template: bool = False


class CTDSectionSummaryApproveRequest(BaseModel):
    """Request payload for approving a CTD section summary."""

    tenant_id: str
    project_id: str
    bucket: str
    final_text: str
    summary_id: Optional[str] = None
    section: Optional[str] = None


class CTDTabulatedSummaryRequest(BaseModel):
    """Request payload for generating a CTD tabulated summary draft."""

    section: str
    tenant_id: str
    project_id: str
    bucket: str
    use_llm: Optional[bool] = True
    user_prompt: Optional[str] = None
    user_comment: Optional[str] = None
    previous_tabulated_id: Optional[str] = None
    refresh_template: bool = False


class CTDTabulatedSummaryApproveRequest(BaseModel):
    """Request payload for approving a CTD tabulated summary."""

    tenant_id: str
    project_id: str
    bucket: str
    final_payload: Dict[str, Any]
    summary_id: Optional[str] = None
    section: Optional[str] = None


try:
    _metadata_generator = OpenAIMetadataGenerator()
except Exception:  # pragma: no cover - optional dependency or missing key
    _metadata_generator = None

try:
    _summary_generator = OpenAIDocumentSummarizer()
except Exception:  # pragma: no cover - optional dependency or missing key
    _summary_generator = None


if SupabaseEmbeddingStore:
    try:
        _embedding_store = SupabaseEmbeddingStore()
    except Exception:  # pragma: no cover - optional dependency or missing key
        _embedding_store = None
else:  # pragma: no cover - optional dependency missing
    _embedding_store = None


_EMBEDDING_CLIENT: Optional[OpenAI] = None


def _get_embedding_client() -> Optional[OpenAI]:
    if OpenAI is None:
        return None
    api_key = os.getenv("OPENAI_API_KEY") or settings.llm_api_key
    if not api_key or api_key == "YOUR_API_KEY":
        return None
    global _EMBEDDING_CLIENT
    if _EMBEDDING_CLIENT is None:
        _EMBEDDING_CLIENT = OpenAI(api_key=api_key)
    return _EMBEDDING_CLIENT


def _generate_embedding(text: str, expected_dim: int = 1536) -> Optional[List[float]]:
    client = _get_embedding_client()
    if not client:
        return None
    try:
        response = client.embeddings.create(
            model=settings.embedding_model_name,
            input=text,
        )
    except Exception as exc:  # pragma: no cover - network/API failure
        logger.warning("Failed to generate section summary embedding: %s", exc)
        return None
    if not response.data:
        return None
    embedding = list(response.data[0].embedding)
    if expected_dim and len(embedding) != expected_dim:
        logger.warning(
            "Embedding dimension mismatch (expected %s, got %s); skipping storage.",
            expected_dim,
            len(embedding),
        )
        return None
    return embedding


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
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
    except ClientError:
        return None
    body = obj.get("Body")
    if not body:
        return None
    return body.read().decode("utf-8")


def _normalize_extra_attributes(value: Any) -> Dict[str, Any]:
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
    try:
        import importlib

        boto3_module = importlib.import_module("boto3")
    except ModuleNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail="boto3 is required for S3 operations",
        ) from exc
    return boto3_module.client(service, region_name=region_name)


def _require_valid_user_id(db: Session, user_id: str) -> str:
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    try:
        normalized = str(uuid.UUID(user_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="user_id must be a valid UUID"
        ) from exc
    row = db.execute(
        sqltext("SELECT 1 FROM users WHERE id = :uid"),
        {"uid": normalized},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="user_id not found")
    return normalized


def _normalize_prefix_list(prefixes: Optional[str]) -> List[str]:
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
    cleaned = prefix.strip().lstrip("/")
    if cleaned and not cleaned.endswith("/"):
        cleaned = f"{cleaned}/"
    return cleaned


def _s3_prefix_exists(s3_client: Any, bucket: str, prefix: str) -> bool:
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


# ---------------------------------------------------------------------------
# Section labeling helpers (Module 2.4 / 2.6 template-driven)
# ---------------------------------------------------------------------------
_IND_TEMPLATE_SECTIONS: List[Dict[str, str]] | None = None
_IND_TEMPLATE_ENTRIES: List[Dict[str, Any]] | None = None


def _reset_ind_template_cache() -> None:
    global _IND_TEMPLATE_SECTIONS, _IND_TEMPLATE_ENTRIES
    _IND_TEMPLATE_SECTIONS = None
    _IND_TEMPLATE_ENTRIES = None


def _load_ind_template_sections() -> List[Dict[str, str]]:
    """Load section numbers/titles from the IND 2.4/2.6 template."""
    global _IND_TEMPLATE_SECTIONS
    if _IND_TEMPLATE_SECTIONS is not None:
        return _IND_TEMPLATE_SECTIONS

    try:
        entries = load_template_entries()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Unable to load IND template: %s", exc)
        _IND_TEMPLATE_SECTIONS = []
        return _IND_TEMPLATE_SECTIONS

    sections: List[Dict[str, str]] = []
    for entry in entries:
        section_id = str(entry.get("Section") or "").strip()
        if not section_id:
            continue
        title = str(
            entry.get("Subsection Header") or entry.get("Section Header") or section_id
        ).strip()
        content_parts = [
            entry.get("Section Header") or "",
            entry.get("Subsection Header") or "",
            entry.get("Content") or "",
        ]
        blob = " ".join(part for part in content_parts if part).strip()
        sections.append({"section": section_id, "title": title, "blob": blob})

    _IND_TEMPLATE_SECTIONS = sections
    return _IND_TEMPLATE_SECTIONS


def _load_ind_template_entries() -> List[Dict[str, Any]]:
    """Load full template entries (section + subsection + headers/content)."""
    global _IND_TEMPLATE_ENTRIES
    if _IND_TEMPLATE_ENTRIES is not None:
        return _IND_TEMPLATE_ENTRIES

    try:
        source_entries = load_template_entries()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Unable to load IND template: %s", exc)
        _IND_TEMPLATE_ENTRIES = []
        return _IND_TEMPLATE_ENTRIES

    entries: List[Dict[str, Any]] = []
    for entry in source_entries:
        if not isinstance(entry, dict):
            continue
        section_id = str(entry.get("Section") or "").strip()
        subsection = str(entry.get("Subsection") or "").strip()
        if not section_id and not subsection:
            continue
        element_raw = str(entry.get("Subsection Element Numbering") or "").strip()
        element_number = normalize_element_number(element_raw)
        entries.append(
            {
                "section": section_id,
                "subsection": subsection or None,
                "element_number": element_number or None,
                "section_header": entry.get("Section Header"),
                "subsection_header": entry.get("Subsection Header"),
                "content": entry.get("Content"),
                "raw": entry,
            }
        )

    _IND_TEMPLATE_ENTRIES = entries
    return _IND_TEMPLATE_ENTRIES


def _fetch_template_overrides(
    db: Session, user_id: str, section: str
) -> List[Dict[str, Any]]:
    """
    Fetch overrides for a user matching a section or subsection prefix.
    Assumes table ncd_template_override(user_id UUID, section TEXT, subsection TEXT, payload JSONB, updated_at TIMESTAMPTZ, created_at TIMESTAMPTZ).
    """
    try:
        rows = (
            db.execute(
                sqltext(
                    """
                    SELECT section, subsection, payload
                    FROM ncd_template_override
                    WHERE user_id = :uid
                      AND (section = :sec OR section LIKE :sec_like)
                """
                ),
                {"uid": user_id, "sec": section, "sec_like": f"{section}%"},
            )
            .mappings()
            .all()
        )
    except Exception as exc:
        logger.warning("Failed to fetch template overrides: %s", exc)
        return []

    overrides: List[Dict[str, Any]] = []
    for row in rows:
        overrides.append(
            {
                "section": row.get("section"),
                "subsection": row.get("subsection"),
                "payload": row.get("payload") or {},
            }
        )
    return overrides


def _upsert_template_override(
    db: Session,
    user_id: str,
    section: str,
    subsection: Optional[str],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Insert or update a template override for a user."""
    existing = db.execute(
        sqltext(
            """
            SELECT id FROM ncd_template_override
            WHERE user_id = :uid
              AND section = :sec
              AND ((subsection IS NULL AND :sub IS NULL) OR subsection = :sub)
            """
        ),
        {"uid": user_id, "sec": section, "sub": subsection},
    ).scalar()

    if existing:
        db.execute(
            sqltext(
                """
                UPDATE ncd_template_override
                SET payload = CAST(:payload AS jsonb), updated_at = now()
                WHERE id = :id
                """
            ),
            {"payload": json.dumps(payload), "id": existing},
        )
        db.commit()
        return {
            "id": str(existing),
            "section": section,
            "subsection": subsection,
            "payload": payload,
        }

    new_id = db.execute(
        sqltext(
            """
            INSERT INTO ncd_template_override (user_id, section, subsection, payload)
            VALUES (:uid, :sec, :sub, CAST(:payload AS jsonb))
            RETURNING id
            """
        ),
        {
            "uid": user_id,
            "sec": section,
            "sub": subsection,
            "payload": json.dumps(payload),
        },
    ).scalar()
    db.commit()
    return {
        "id": str(new_id),
        "section": section,
        "subsection": subsection,
        "payload": payload,
    }


def _match_section_regex(
    text: str, sections: List[Dict[str, str]]
) -> Tuple[str, str, float] | None:
    """Find direct section-number mentions in text."""
    for entry in sections:
        sec = entry["section"]
        if not sec:
            continue
        pattern = rf"\b{re.escape(sec)}\b"
        if re.search(pattern, text):
            return sec, entry["title"], 0.98
    return None


def _score_sections_similarity(
    text: str, sections: List[Dict[str, str]]
) -> Tuple[str, str, float] | None:
    """Score sections using fuzzy similarity against headers/content."""
    if not text.strip():
        return None
    sample = text.lower()[:8000]
    best: Tuple[str, str, float] | None = None
    for entry in sections:
        title = entry["title"].lower()
        blob = entry["blob"].lower() if entry["blob"] else title
        score = (
            max(fuzz.partial_ratio(sample, title), fuzz.partial_ratio(sample, blob))
            / 100.0
        )
        if best is None or score > best[2]:
            best = (entry["section"], entry["title"], round(score, 3))
    return best


def _guess_section_from_name(
    name: str, sections: List[Dict[str, str]]
) -> Tuple[str, str, float] | None:
    """Infer section directly from filename/key if it contains a number."""
    if not name:
        return None
    candidates = re.findall(r"\b\d+(?:\.\d+)+\b", name)
    if not candidates:
        return None
    # Pick the longest/most specific section string
    candidates.sort(key=lambda s: (s.count("."), len(s)), reverse=True)
    section_numbers = [entry["section"] for entry in sections]
    for cand in candidates:
        for entry in sections:
            if entry["section"] == cand:
                return cand, entry["title"], 0.99
        # No exact template match; compute similarity against known numbers to set confidence
        best_num_score = 0.0
        for sec in section_numbers:
            best_num_score = max(best_num_score, fuzz.partial_ratio(cand, sec) / 100.0)
        depth_bonus = min(0.2, cand.count(".") * 0.05)
        confidence = round(min(0.95, max(best_num_score, 0.5) + depth_bonus), 3)
        return cand, cand, confidence
    return None


def _top_section_candidates(
    text: str, sections: List[Dict[str, str]], limit: int = 5
) -> List[Dict[str, Any]]:
    """Return top candidate matches for debugging/QA."""
    sample = text.lower()[:8000]
    scored: List[Tuple[float, Dict[str, str]]] = []
    for entry in sections:
        title = entry["title"].lower()
        blob = entry["blob"].lower() if entry["blob"] else title
        score = (
            max(fuzz.partial_ratio(sample, title), fuzz.partial_ratio(sample, blob))
            / 100.0
        )
        scored.append((score, entry))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = []
    for score, entry in scored[:limit]:
        top.append(
            {
                "section_number": entry["section"],
                "section_title": entry["title"],
                "score": round(score, 3),
            }
        )
    return top


def _llm_select_section(
    text: str, sections: List[Dict[str, str]]
) -> Tuple[str, str, float] | None:
    """Optional LLM-based selection constrained to known sections."""
    if not _metadata_generator or not getattr(_metadata_generator, "_client", None):
        return None
    client = _metadata_generator._client  # type: ignore[attr-defined]
    options = "\n".join(f"- {s['section']}: {s['title']}" for s in sections[:120])
    system_prompt = (
        "You are a regulatory assistant classifying Module 2.4/2.6 documents. "
        "Choose the single best matching section number from the provided options. "
        "Respond ONLY with minified JSON: "
        '{"section_number":"<number>","section_title":"<title>","confidence":0.0} '
        "where confidence is 0.0-1.0. Use only the supplied section numbers."
    )
    user_prompt = f"Options:\n{options}\n\n" "PDF excerpt (trimmed):\n" f"{text[:4000]}"
    try:
        response = client.responses.create(
            model=getattr(_metadata_generator, "model", "gpt-4o-mini"),  # type: ignore[attr-defined]
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
        )
    except Exception as exc:  # pragma: no cover - network or auth errors
        logger.warning("LLM section selection failed: %s", exc)
        return None

    raw_text = _extract_text_from_response(response)
    if not raw_text:
        return None
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.warning("LLM section response was not valid JSON: %s", raw_text)
        return None

    sec = str(data.get("section_number") or "").strip()
    title = str(data.get("section_title") or "").strip()
    conf = data.get("confidence")
    try:
        confidence = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        confidence = None

    if not sec:
        return None
    if not title:
        try:
            title = next((s["title"] for s in sections if s["section"] == sec), "")
        except StopIteration:
            title = ""
    return sec, title, round(confidence or 0.0, 3)


def _classify_section_from_text(
    text: str, *, filename: Optional[str] = None, use_llm: bool = False
) -> Tuple[str, str, float, str] | None:
    """Determine the best section using filename hints + minimal text."""
    sections = _load_ind_template_sections()
    if not sections:
        return None

    if filename:
        guessed = _guess_section_from_name(filename, sections)
        if guessed:
            return guessed[0], guessed[1], guessed[2], "filename"

    direct = _match_section_regex(text, sections)
    if direct:
        return direct[0], direct[1], direct[2], "regex"

    heuristic = _score_sections_similarity(text, sections)
    best = heuristic
    method = "heuristic"

    if use_llm:
        llm_pick = _llm_select_section(text, sections)
        if llm_pick:
            best = llm_pick
            method = "llm"

    if best:
        return best[0], best[1], best[2], method
    return None


def _parse_s3_context(key: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Best-effort attempt to infer company/project/module from a key path."""
    segments = key.split("/")
    company = segments[0] if len(segments) > 0 else None
    project = segments[1] if len(segments) > 1 else None
    module_label = segments[2] if len(segments) > 2 else None
    return company, project, module_label


def _store_embedding_for_document(
    *,
    bucket: str,
    key: str,
    version_id: Optional[str],
    filename: str,
    markdown: str,
    metadata_fields: Dict[str, Any],
) -> None:
    """Optionally persist embeddings for markdown output if the store is configured."""
    if not _embedding_store or not markdown:
        return
    company, project, module_label = _parse_s3_context(key)
    try:
        _embedding_store.store_document(
            s3_bucket=bucket,
            s3_key=key,
            version_id=version_id,
            filename=filename,
            markdown=markdown,
            metadata=metadata_fields,
            company=company,
            project=project,
            module_label=module_label,
            labels=metadata_fields.get("labels"),
            keywords=metadata_fields.get("keywords"),
            language=metadata_fields.get("language"),
        )
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Embedding storage failed for %s: %s", key, exc)


def _normalise_table_engines(
    table_engine: str | Sequence[str] | None,
) -> List[str]:
    """Normalise user input into a list of table engine identifiers."""
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
    """Run table extraction across engines and return the most productive result."""
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
    """Split a path-like string while removing unsafe/empty segments."""
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
    """Construct a canonical S3 key based on company/project/folder information."""
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
    """Run the full analysis pipeline and persist metadata/analysis sidecars."""
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "boto3 is required for S3 operations. Install the 'infra' extras."
        ) from exc

    s3_client = _boto3_client("s3", region_name=aws_region)

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

    markdown_text = analysis_result.get("markdown") or ""
    if markdown_text:
        _store_embedding_for_document(
            bucket=bucket,
            key=key,
            version_id=version_id,
            filename=filename,
            markdown=markdown_text,
            metadata_fields=metadata_fields,
        )

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
            raise HTTPException(
                status_code=status, detail=f"Failed to fetch S3 object: {exc}"
            ) from exc

        try:
            sample_text, pages_sampled = _extract_sample_text(tmp_path, page_limit)
            classification = _classify_section_from_text(
                sample_text,
                filename=Path(payload.key).name,
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
            raise HTTPException(
                status_code=502, detail=f"Failed to copy PDF to labeled folder: {exc}"
            ) from exc

        dest_version_id = copy_resp.get("VersionId")
        meta_fields: Dict[str, Any] = {
            "ind_section_number": section_number,
            "ind_section_title": section_title,
            "ind_classification_confidence": confidence,
            "classification_method": method,
            "source_key": payload.key,
            "pages_sampled": pages_sampled,
        }
        if section_number:
            meta_fields["labels"] = [f"section:{section_number}"]

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
        raise HTTPException(status_code=500, detail=f"Labeling failed: {exc}") from exc


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
        raise HTTPException(
            status_code=502, detail=f"Failed to copy PDF to relabeled folder: {exc}"
        ) from exc

    # Best-effort move: delete the original after successful copy
    try:
        s3_client.delete_object(Bucket=bucket, Key=payload.key)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning(
            "Failed to delete original after relabel (%s): %s", payload.key, exc
        )

    dest_version_id = copy_resp.get("VersionId")
    meta_fields: Dict[str, Any] = {
        "ind_section_number": payload.section_number,
        "ind_section_title": payload.section_title or payload.section_number,
        "classification_method": "manual",
        "source_key": payload.key,
        "labels": [f"section:{payload.section_number}"],
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
        "section_title": meta_fields["ind_section_title"],
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
) -> Dict[str, Any]:
    """
    Dev-only: label a local PDF upload without S3 side-effects.
    Returns the predicted section, confidence, method, and top fuzzy candidates.
    """
    filename = file.filename or "uploaded.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    with NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp_path = Path(tmp.name)
        try:
            shutil.copyfileobj(file.file, tmp)
            tmp.flush()
        finally:
            file.file.close()

    try:
        sample_text, pages_sampled = _extract_sample_text(tmp_path, page_limit)
        classification = _classify_section_from_text(
            sample_text,
            filename=filename,
            use_llm=use_llm,
        )
        candidates = _top_section_candidates(
            sample_text, _load_ind_template_sections(), limit=5
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
        raise HTTPException(
            status_code=500, detail=f"Failed to upsert template override: {exc}"
        ) from exc
    finally:
        db.close()


@ncd_router.get("/template")
async def get_template_sections(
    section: Optional[str] = None, user_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Return template entries for a given section or subsection from ind_24_26_template.json.
    - If section is omitted, returns the full template JSON payload.
    - If you pass a subsection (e.g., 2.4.1-A), returns the matching entry.
    - If you pass a parent section (e.g., 2.4.1), returns all subsection entries under it.
    - If user_id is provided and overrides exist, they are merged (override wins).
    """
    if not section or not section.strip():
        template_path = resolve_template_path()
        if not template_path:
            raise HTTPException(status_code=404, detail="Template file not found")
        try:
            payload = json.loads(template_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=500, detail=f"Template file is invalid JSON: {exc}"
            ) from exc
        return {"template": payload}

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

    # Apply overrides if user_id provided
    if user_id:
        db = SessionLocal()
        try:
            overrides = _fetch_template_overrides(db, user_id, target)
        finally:
            db.close()

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
            # Add overrides not present in defaults
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

    return {"section": target, "entries": matches}


@ncd_router.get("/templates")
async def list_template_downloads(
    user_id: str,
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
        raise HTTPException(
            status_code=500, detail=f"Failed to check template folder: {exc}"
        ) from exc
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
            raise HTTPException(
                status_code=500, detail=f"Failed to list templates: {exc}"
            ) from exc
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


@ncd_router.get("/ctd/2.4/element")
async def get_ctd_element_reference(
    element: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
    refresh: bool = False,
) -> Dict[str, Any]:
    normalized = normalize_element_number(element)
    if not normalized:
        raise HTTPException(status_code=400, detail="element is required")
    if not normalized.startswith("2.4."):
        raise HTTPException(status_code=400, detail="element must start with 2.4.")
    if not tenant_id or not project_id or not bucket:
        raise HTTPException(
            status_code=400, detail="tenant_id, project_id, and bucket are required"
        )

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


def _truncate_text(text: Optional[str], max_chars: int = 4000) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + " ...[truncated]"


def _normalize_summary(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return ""
        try:
            return json.loads(trimmed)
        except json.JSONDecodeError:
            return trimmed
    return value


def _summary_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=True)


def _trim_section_summary_context(context: Dict[str, Any]) -> Dict[str, Any]:
    def _trim_sources(
        sources: Sequence[Dict[str, Any]],
        *,
        limit: int,
        max_summary_chars: int,
    ) -> List[Dict[str, Any]]:
        trimmed: List[Dict[str, Any]] = []
        for row in sources[:limit]:
            if not isinstance(row, dict):
                continue
            slim = dict(row)
            slim["summary_text"] = _truncate_text(
                str(slim.get("summary_text") or ""), max_summary_chars
            )
            if "summary" in slim:
                slim["summary"] = None
            trimmed.append(slim)
        return trimmed

    def _trim_key_sections(
        sections: Sequence[Dict[str, Any]],
        *,
        limit: int,
        max_text_chars: int,
    ) -> List[Dict[str, Any]]:
        trimmed: List[Dict[str, Any]] = []
        for row in sections[:limit]:
            if not isinstance(row, dict):
                continue
            slim = dict(row)
            slim["text"] = _truncate_text(str(slim.get("text") or ""), max_text_chars)
            trimmed.append(slim)
        return trimmed

    def _trim_elements(
        elements: Sequence[Dict[str, Any]],
        *,
        limit: int,
        max_summary_chars: int,
        max_text_chars: int,
    ) -> List[Dict[str, Any]]:
        trimmed: List[Dict[str, Any]] = []
        for row in elements[:limit]:
            if not isinstance(row, dict):
                continue
            slim = dict(row)
            slim["sources"] = _trim_sources(
                slim.get("sources") or [],
                limit=3,
                max_summary_chars=max_summary_chars,
            )
            slim["key_sections"] = _trim_key_sections(
                slim.get("key_sections") or [],
                limit=3,
                max_text_chars=max_text_chars,
            )
            trimmed.append(slim)
        return trimmed

    def _build_trimmed(
        *,
        source_limit: int,
        key_limit: int,
        element_limit: int,
        summary_chars: int,
        key_chars: int,
    ) -> Dict[str, Any]:
        trimmed = dict(context)
        trimmed["section_sources"] = _trim_sources(
            context.get("section_sources") or [],
            limit=source_limit,
            max_summary_chars=summary_chars,
        )
        trimmed["section_key_sections"] = _trim_key_sections(
            context.get("section_key_sections") or [],
            limit=key_limit,
            max_text_chars=key_chars,
        )
        trimmed["elements"] = _trim_elements(
            context.get("elements") or [],
            limit=element_limit,
            max_summary_chars=summary_chars,
            max_text_chars=key_chars,
        )
        return trimmed

    for source_limit, key_limit, element_limit, summary_chars, key_chars in (
        (20, 20, 10, 1200, 1200),
        (10, 10, 5, 800, 800),
        (5, 5, 3, 600, 600),
    ):
        candidate = _build_trimmed(
            source_limit=source_limit,
            key_limit=key_limit,
            element_limit=element_limit,
            summary_chars=summary_chars,
            key_chars=key_chars,
        )
        if (
            len(json.dumps(candidate, ensure_ascii=True, default=str))
            <= SECTION_PROMPT_MAX_CHARS
        ):
            return candidate

    minimal = dict(context)
    minimal["section_sources"] = _trim_sources(
        context.get("section_sources") or [],
        limit=5,
        max_summary_chars=400,
    )
    minimal["section_key_sections"] = []
    minimal["elements"] = []
    return minimal


def _normalize_json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _require_uuid(value: str, field_name: str) -> str:
    if not value:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a UUID")
    return value


def _normalize_optional_uuid(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a UUID")
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        uuid.UUID(cleaned)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a UUID")
    return cleaned


def _normalize_uuid_list(values: Sequence[Any]) -> List[str]:
    normalized: List[str] = []
    for value in values:
        if value is None:
            continue
        try:
            normalized.append(str(uuid.UUID(str(value))))
        except (ValueError, TypeError):
            continue
    return normalized


def _slim_sources(sources: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    payload: List[Dict[str, Any]] = []
    for row in sources:
        summary = _normalize_summary(row.get("summary") or row.get("summary_text"))
        payload.append(
            {
                "section_number": row.get("section_number"),
                "section_title": row.get("section_title"),
                "summary": summary,
                "summary_text": _summary_to_text(summary),
                "keywords": row.get("keywords") or [],
                "summary_type": row.get("summary_type"),
                "summary_purpose": row.get("summary_purpose"),
                "document_section_id": row.get("document_section_id")
                or row.get("section_id"),
                "document_version_id": row.get("document_version_id"),
                "document_id": row.get("document_id"),
            }
        )
    return payload


def _slim_key_sections(sections: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    payload: List[Dict[str, Any]] = []
    for row in sections:
        payload.append(
            {
                "section_type": row.get("section_type"),
                "text": _truncate_text(row.get("text")),
                "page_start": row.get("page_start"),
                "page_end": row.get("page_end"),
                "char_start": row.get("char_start"),
                "char_end": row.get("char_end"),
                "asset_ids": row.get("asset_ids") or [],
                "document_version_id": row.get("document_version_id"),
            }
        )
    return payload


def _template_entries_for_section(section: str) -> List[Dict[str, Any]]:
    entries = _load_ind_template_entries()
    matches: List[Dict[str, Any]] = []
    target = section.strip()
    if not target:
        return matches
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
    return matches


def _element_entries_for_section(section: str) -> List[str]:
    if section.strip() != "2.4.5":
        return []
    return [f"{section}-a", f"{section}-b", f"{section}-c"]


def _build_section_summary_context(
    db: Session,
    *,
    section: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
) -> tuple[Dict[str, Any], List[str]]:
    project_name = fetch_project_name(db, project_id)
    project_like = f"%/{project_name}/%" if project_name else "%"

    module4_sections, mapping_entries, targets = module4_sections_for_ctd_targets(
        section
    )
    if section.startswith("2.6."):
        exact_sections = set()
        filtered_mappings: List[Dict[str, Any]] = []
        for entry in mapping_entries:
            matched_targets = [
                target_entry
                for target_entry in entry.get("targets", [])
                if target_entry.get("section") == section
            ]
            if matched_targets:
                filtered = dict(entry)
                filtered["matched_targets"] = matched_targets
                filtered_mappings.append(filtered)
                exact_sections.add(entry.get("module4_section"))
        if filtered_mappings:
            module4_sections = sorted(s for s in exact_sections if s)
            mapping_entries = filtered_mappings

    sources = fetch_section_sources(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
        module4_sections=module4_sections,
    )
    project_document_keys = fetch_project_document_keys(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
    )
    document_keys = fetch_document_keys_for_sections(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
        module4_sections=module4_sections,
    )
    key_sections = fetch_key_sections_for_sections(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
        module4_sections=module4_sections,
        section_type=None,
    )

    element_numbers = _element_entries_for_section(section)
    element_payloads: List[Dict[str, Any]] = []
    used_elements: List[str] = []
    for element in element_numbers:
        try:
            payload = build_ctd_element_reference(
                db,
                tenant_id=tenant_id,
                project_id=project_id,
                bucket=bucket,
                element_number=element,
                include_assets=False,
                include_key_sections=True,
            )
        except ValueError:
            continue
        element_payloads.append(
            {
                "element_number": payload.get("element_number"),
                "section_number": payload.get("section_number"),
                "module4_sections": payload.get("module4_sections") or [],
                "template": payload.get("template") or {},
                "sources": _slim_sources(payload.get("sources") or []),
                "key_sections": _slim_key_sections(payload.get("key_sections") or []),
            }
        )
        if payload.get("element_number"):
            used_elements.append(str(payload.get("element_number")))

    context = {
        "section": section,
        "ctd_targets": targets,
        "module4_sections": module4_sections,
        "mapping": mapping_entries,
        "template_entries": _template_entries_for_section(section),
        "section_sources": _slim_sources([dict(row) for row in sources]),
        "section_key_sections": _slim_key_sections([dict(row) for row in key_sections]),
        "elements": element_payloads,
    }
    return context, used_elements


def _format_embedding_for_prompt(embedding: Any) -> str:
    if not embedding:
        return ""
    values: List[float] = []
    if isinstance(embedding, str):
        cleaned = embedding.strip()
        if cleaned.startswith("[") and cleaned.endswith("]"):
            cleaned = cleaned[1:-1]
        if cleaned:
            try:
                values = [float(value) for value in cleaned.split(",") if value.strip()]
            except ValueError:
                values = []
    else:
        try:
            values = [float(value) for value in embedding]
        except (TypeError, ValueError):
            values = []
    if not values:
        return ""
    return "[" + ",".join(f"{value:.6f}" for value in values) + "]"


def _build_section_summary_prompt(
    *,
    section: str,
    context: Dict[str, Any],
    user_prompt: Optional[str],
    user_comment: Optional[str],
    previous_summary: Optional[str],
    previous_embedding: Any,
) -> tuple[str, str]:
    system_prompt = (
        "You are an expert nonclinical regulatory writer. "
        "Generate CTD Module 2.4/2.6 section summaries using only the provided data. "
        "Do not invent data. If key data is missing, state what is missing."
    )
    instructions = (
        user_prompt.strip()
        if user_prompt
        else (
            "Use the template guidance and data to produce a concise section summary."
        )
    )
    parts = [
        f"CTD Section: {section}",
        f"Instructions: {instructions}",
    ]
    if user_comment:
        if previous_summary:
            parts.append(f"Previous summary:\n{previous_summary}")
        embedding_text = _format_embedding_for_prompt(previous_embedding)
        if embedding_text:
            parts.append(f"Previous summary embedding: {embedding_text}")
        parts.append(f"User comment:\n{user_comment}")
        parts.append("Revise the summary to address the comment.")

    context_json = json.dumps(
        _trim_section_summary_context(context), ensure_ascii=True, indent=2, default=str
    )
    parts.append(f"Context data (JSON):\n{context_json}")
    return system_prompt, "\n\n".join(parts)


def _parse_columns_header(value: Any) -> List[str]:
    if not isinstance(value, str):
        return []
    separator = ";"
    if ";" not in value and "|" in value:
        separator = "|"
    return [item.strip() for item in value.split(separator) if item.strip()]


def _truncate_preview_value(value: Any, max_len: int = 300) -> str:
    text = str(value) if value is not None else ""
    if len(text) <= max_len:
        return text
    return f"{text[:max_len].rstrip()}..."


def _trim_tabulated_context(context: Dict[str, Any]) -> Dict[str, Any]:
    max_assets = 8
    max_preview_rows = 6
    max_columns = 12
    trimmed_assets: List[Dict[str, Any]] = []
    for asset in (context.get("table_assets") or [])[:max_assets]:
        if not isinstance(asset, dict):
            continue
        preview_rows = []
        for row in (asset.get("preview_rows") or [])[:max_preview_rows]:
            if not isinstance(row, dict):
                continue
            preview_rows.append({k: _truncate_preview_value(v) for k, v in row.items()})
        trimmed_assets.append(
            {
                "id": asset.get("id"),
                "s3_key": asset.get("s3_key"),
                "json_key": asset.get("json_key"),
                "caption": asset.get("caption"),
                "description": asset.get("description"),
                "page_number": asset.get("page_number"),
                "index_on_page": asset.get("index_on_page"),
                "columns": (asset.get("columns") or [])[:max_columns],
                "row_count": asset.get("row_count"),
                "preview_rows": preview_rows,
            }
        )
    return {
        "section": context.get("section"),
        "ctd_targets": context.get("ctd_targets") or [],
        "module4_sections": (context.get("module4_sections") or [])[:20],
        "table_specs": context.get("table_specs") or [],
        "table_assets": trimmed_assets,
    }


def _extract_study_ids(text: str) -> List[str]:
    if not text:
        return []
    found: List[str] = []
    for match in STUDY_ID_RE.findall(text):
        if not match:
            continue
        cleaned = STUDY_ID_EXT_RE.sub("", match)
        cleaned = STUDY_ID_PREFIX_RE.sub("", cleaned)
        cleaned = cleaned.strip("._-")
        if not cleaned:
            continue
        while "." in cleaned:
            parts = cleaned.split(".")
            if parts[-1].lower() not in STUDY_ID_TRAILERS:
                break
            cleaned = ".".join(parts[:-1]).strip("._-")
            if not cleaned:
                break
        if not cleaned:
            continue
        if STUDY_ID_SKIP_RE.match(cleaned):
            continue
        if len(cleaned) < 6:
            continue
        if sum(1 for ch in cleaned if ch.isdigit()) < 3:
            continue
        if not STUDY_ID_RE.fullmatch(cleaned):
            continue
        found.append(cleaned)
    seen = set()
    ordered: List[str] = []
    for value in found:
        key = value.upper()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(value)
    return ordered


def _build_study_id_candidates(context: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    candidates: Dict[str, Dict[str, Any]] = {}

    def add_candidate(study_id: str, label: str) -> None:
        key = study_id.upper()
        entry = candidates.setdefault(key, {"study_id": study_id, "labels": set()})
        if label:
            entry["labels"].add(label)

    for asset in context.get("table_assets", []) or []:
        if not isinstance(asset, dict):
            continue
        parts = [
            asset.get("s3_key"),
            asset.get("json_key"),
            asset.get("caption"),
            asset.get("description"),
        ]
        keywords = asset.get("keywords") or []
        if isinstance(keywords, list):
            parts.extend(str(item) for item in keywords if item)
        blob = " ".join(str(part) for part in parts if part)
        for study_id in _extract_study_ids(blob):
            add_candidate(study_id, blob)

    for source in context.get("section_sources", []) or []:
        if not isinstance(source, dict):
            continue
        blob = " ".join(
            str(part)
            for part in (
                source.get("section_title"),
                source.get("section_number"),
                source.get("s3_key"),
                source.get("summary_text"),
            )
            if part
        )
        for study_id in _extract_study_ids(blob):
            add_candidate(study_id, blob)

    for key in context.get("document_keys", []) or []:
        if not key:
            continue
        for study_id in _extract_study_ids(str(key)):
            add_candidate(study_id, key)

    for key in context.get("s3_listing_keys", []) or []:
        if not key:
            continue
        for study_id in _extract_study_ids(str(key)):
            add_candidate(study_id, key)

    for study_id in context.get("ncd_study_ids", []) or []:
        if not study_id:
            continue
        add_candidate(str(study_id), "ncd_study")

    return candidates


def _select_study_id_from_text(
    text: str, candidates: Dict[str, Dict[str, Any]]
) -> Optional[str]:
    if not text:
        return None
    for study_id in _extract_study_ids(text):
        key = study_id.upper()
        if key in candidates:
            return candidates[key]["study_id"]
        return study_id

    best_id = None
    best_score = 0
    for entry in candidates.values():
        labels = entry.get("labels") or []
        for label in labels:
            score = fuzz.token_set_ratio(text, label)
            if score > best_score:
                best_score = score
                best_id = entry.get("study_id")
    if best_id and best_score >= 70:
        return best_id
    return None


def _normalize_tabulated_study_ids(
    tables: Sequence[Dict[str, Any]],
    context: Dict[str, Any],
) -> None:
    candidates = _build_study_id_candidates(context)
    if not candidates:
        return

    def pick_column(columns: Sequence[str], options: Sequence[str]) -> Optional[str]:
        for column in columns:
            normalized = column.strip().lower()
            for option in options:
                if normalized == option:
                    return column
        return None

    for table in tables:
        if not isinstance(table, dict):
            continue
        columns = table.get("columns") or []
        if not isinstance(columns, list):
            continue
        study_col = pick_column(
            columns,
            (
                "study number",
                "study id",
                "study no.",
                "study no",
                "study #",
                "study number [-]",
                "study id [-]",
            ),
        )
        if not study_col:
            continue
        location_col = pick_column(
            columns,
            (
                "location in ctd",
                "location in ctd: vol. section",
                "location in ctd: vol. section [-]",
                "location in ctd [-]",
            ),
        )
        for row in table.get("rows") or []:
            if not isinstance(row, dict):
                continue
            current = str(row.get(study_col) or "").strip()
            if current and current.lower() in {
                "none",
                "none conducted",
                "not conducted",
            }:
                continue
            if current and _extract_study_ids(current):
                normalized = _select_study_id_from_text(current, candidates)
                if normalized:
                    row[study_col] = normalized
                continue
            location_value = (
                str(row.get(location_col) or "").strip() if location_col else ""
            )
            normalized = _select_study_id_from_text(location_value, candidates)
            if not normalized:
                row_text = " ".join(
                    str(row.get(col) or "") for col in columns if col != study_col
                ).strip()
                normalized = _select_study_id_from_text(row_text, candidates)
            if normalized:
                row[study_col] = normalized


def _normalize_header_token(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    alias_map = {
        "dose": "doses",
        "dose level": "doses",
        "dose levels": "doses",
        "dose mg kg": "doses",
        "dose mg/kg": "doses",
        "doses mg kg": "doses",
        "doses mg/kg": "doses",
        "dosage": "doses",
        "route of administration": "method of administration",
        "route": "method of administration",
        "administration route": "method of administration",
        "method of admin": "method of administration",
        "study id": "study number",
        "study no": "study number",
        "study no.": "study number",
        "study #": "study number",
        "species strain": "species strain",
        "species strain or test system": "species strain or test system",
        "species strain or test system or species": "species strain or test system",
        "organ system": "organ systems evaluated",
        "organ systems": "organ systems evaluated",
        "organ systems evaluated": "organ systems evaluated",
        "glp": "glp compliance",
        "sex": "gender",
        "gender and no per group": "gender and no per group",
        "gender and no. per group": "gender and no per group",
        "sex and no per group": "gender and no per group",
        "sex and no. per group": "gender and no per group",
        "noteworthy findings": "noteworthy findings",
        "key findings": "noteworthy findings",
        "key results": "noteworthy findings",
        "findings": "noteworthy findings",
        "observations": "noteworthy findings",
    }
    if cleaned in alias_map:
        return alias_map[cleaned]
    if cleaned == "method of admin":
        return "method of administration"
    if cleaned == "method of administrationistration":
        return "method of administration"
    if cleaned == "method of admin istration":
        return "method of administration"
    if cleaned.startswith("dose "):
        return "doses"
    if cleaned.startswith("doses "):
        return "doses"
    if cleaned.startswith("organ system"):
        return "organ systems evaluated"
    return cleaned


def _tokenize_text(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _infer_project_prefix(
    project_name: Optional[str], seeds: Sequence[str]
) -> Optional[str]:
    if not project_name or not seeds:
        return None
    needle = f"/{project_name.lower()}/"
    for key in seeds:
        if not key:
            continue
        lower = str(key).lower()
        idx = lower.find(needle)
        if idx == -1:
            continue
        end = idx + len(needle)
        return str(key)[:end]
    return None


def _list_module4_study_keys(
    s3_client: Any,
    *,
    bucket: str,
    project_name: Optional[str],
    module4_sections: Sequence[str],
    seeds: Sequence[str],
    max_keys: int = 2000,
) -> List[str]:
    prefix = _infer_project_prefix(project_name, seeds)
    if not prefix or not module4_sections:
        return []
    search_prefix = f"{prefix}Module 4 Nonclinical Study Reports/"
    keys: List[str] = []
    key_set: set[str] = set()
    token: Optional[str] = None
    scanned = 0
    module_tokens = [str(section) for section in module4_sections if section]

    def add_key(key: str) -> None:
        if key in key_set:
            return
        key_set.add(key)
        keys.append(key)

    def list_prefix(prefix_value: str) -> None:
        nonlocal scanned
        continuation: Optional[str] = None
        while True:
            params = {
                "Bucket": bucket,
                "Prefix": prefix_value,
                "MaxKeys": 1000,
            }
            if continuation:
                params["ContinuationToken"] = continuation
            try:
                response = s3_client.list_objects_v2(**params)
            except Exception:
                return
            contents = response.get("Contents") or []
            for item in contents:
                key = item.get("Key")
                if not key:
                    continue
                scanned += 1
                if scanned > max_keys:
                    return
                if module_tokens and not any(token in key for token in module_tokens):
                    continue
                if _extract_study_ids(key):
                    add_key(key)
            if not response.get("IsTruncated"):
                break
            continuation = response.get("NextContinuationToken")
            if not continuation:
                break

    seed_prefixes: set[str] = set()
    for seed in seeds:
        if not seed:
            continue
        parts = str(seed).split("/")
        for idx, segment in enumerate(parts):
            if not any(token in segment for token in module_tokens):
                continue
            candidate = "/".join(parts[: idx + 1]) + "/"
            if candidate.startswith(prefix):
                seed_prefixes.add(candidate)
    for seed_prefix in sorted(seed_prefixes):
        list_prefix(seed_prefix)
        if scanned > max_keys:
            return keys

    list_prefix(search_prefix)
    return keys


def _pick_first_value(
    row: Dict[str, Any], key_map: Dict[str, str], *candidates: str
) -> str:
    for candidate in candidates:
        key = key_map.get(candidate)
        if key:
            return str(row.get(key) or "").strip()
    return ""


def _extract_primary_pd_candidates(context: Dict[str, Any]) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    seen: set[str] = set()
    for asset in context.get("table_assets", []) or []:
        preview_rows = asset.get("preview_rows") or []
        for row in preview_rows:
            if not isinstance(row, dict):
                continue
            key_map = {_normalize_header_token(str(key)): key for key in row.keys()}
            if "type of study" not in key_map or not (
                key_map.get("species strain") or key_map.get("test system")
            ):
                continue
            study_number = _pick_first_value(row, key_map, "study number")
            if not study_number or study_number in seen:
                continue
            doses_key = key_map.get("doses")
            candidates.append(
                {
                    "type_of_study": _pick_first_value(row, key_map, "type of study"),
                    "species_strain": _pick_first_value(
                        row,
                        key_map,
                        "species strain",
                        "test system",
                        "species strain or test system",
                    ),
                    "method_of_admin": _pick_first_value(
                        row, key_map, "method of administration"
                    ),
                    "doses": (
                        str(row.get(doses_key or "") or "").strip() if doses_key else ""
                    ),
                    "gender_group": _pick_first_value(
                        row, key_map, "gender and no per group", "gender", "sex"
                    ),
                    "findings": _pick_first_value(
                        row,
                        key_map,
                        "noteworthy findings",
                        "key findings",
                        "key results",
                    ),
                    "study_number": study_number,
                }
            )
            seen.add(study_number)
    return candidates


def _repair_primary_pharmacodynamics_table(
    tables: Sequence[Dict[str, Any]],
    context: Dict[str, Any],
) -> None:
    target = next(
        (table for table in tables if str(table.get("subsection") or "") == "2.6.3.2"),
        None,
    )
    if not target:
        return
    candidates = _extract_primary_pd_candidates(context)
    if not candidates:
        return
    columns = [
        "Type of Study",
        "Species/Strain",
        "Method of Admin.",
        "Doses (mg/kg)",
        "Gender and No. per Group",
        "Noteworthy Findings",
        "Study Number",
    ]
    target["columns"] = columns
    target["rows"] = [
        {
            "Type of Study": candidate.get("type_of_study", ""),
            "Species/Strain": candidate.get("species_strain", ""),
            "Method of Admin.": candidate.get("method_of_admin", ""),
            "Doses (mg/kg)": candidate.get("doses", ""),
            "Gender and No. per Group": candidate.get("gender_group", ""),
            "Noteworthy Findings": candidate.get("findings", ""),
            "Study Number": candidate.get("study_number", ""),
        }
        for candidate in candidates
    ]


def _extract_safety_pharmacology_candidates(
    context: Dict[str, Any]
) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    seen: set[str] = set()
    for asset in context.get("table_assets", []) or []:
        preview_rows = asset.get("preview_rows") or []
        for row in preview_rows:
            if not isinstance(row, dict):
                continue
            key_map = {_normalize_header_token(str(key)): key for key in row.keys()}
            if (
                "organ systems evaluated" not in key_map
                or "glp compliance" not in key_map
            ):
                continue
            study_number = _pick_first_value(row, key_map, "study number")
            if not study_number or study_number in seen:
                continue
            doses_key = key_map.get("doses")
            candidates.append(
                {
                    "organ_systems": _pick_first_value(
                        row, key_map, "organ systems evaluated"
                    ),
                    "species_strain": _pick_first_value(row, key_map, "species strain"),
                    "method_of_admin": _pick_first_value(
                        row, key_map, "method of administration"
                    ),
                    "doses": (
                        str(row.get(doses_key or "") or "").strip() if doses_key else ""
                    ),
                    "gender_group": _pick_first_value(
                        row, key_map, "gender and no per group", "gender", "sex"
                    ),
                    "findings": _pick_first_value(row, key_map, "noteworthy findings"),
                    "glp": _pick_first_value(row, key_map, "glp compliance"),
                    "study_number": study_number,
                }
            )
            seen.add(study_number)
    return candidates


def _repair_safety_pharmacology_table(
    tables: Sequence[Dict[str, Any]],
    context: Dict[str, Any],
) -> None:
    target = next(
        (table for table in tables if str(table.get("subsection") or "") == "2.6.3.4"),
        None,
    )
    if not target:
        return
    candidates = _extract_safety_pharmacology_candidates(context)
    if not candidates:
        return
    columns = [
        "Organ Systems Evaluated",
        "Species/Strain",
        "Method of Admin.",
        "Doses (mg/kg)",
        "Gender and No. per Group",
        "Noteworthy Findings",
        "GLP Compliance",
        "Study Number",
    ]
    target["columns"] = columns
    target["rows"] = [
        {
            "Organ Systems Evaluated": candidate.get("organ_systems", ""),
            "Species/Strain": candidate.get("species_strain", ""),
            "Method of Admin.": candidate.get("method_of_admin", ""),
            "Doses (mg/kg)": candidate.get("doses", ""),
            "Gender and No. per Group": candidate.get("gender_group", ""),
            "Noteworthy Findings": candidate.get("findings", ""),
            "GLP Compliance": candidate.get("glp", ""),
            "Study Number": candidate.get("study_number", ""),
        }
        for candidate in candidates
    ]


def _extract_overview_candidates(context: Dict[str, Any]) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    seen: set[str] = set()
    for asset in context.get("table_assets", []) or []:
        preview_rows = asset.get("preview_rows") or []
        for row in preview_rows:
            if not isinstance(row, dict):
                continue
            key_map = {_normalize_header_token(str(key)): key for key in row.keys()}
            study_key = key_map.get("study number")
            if not study_key:
                for fallback in ("study id", "study no", "study #", "report number"):
                    study_key = key_map.get(fallback)
                    if study_key:
                        break
            type_key = key_map.get("type of study")
            if not type_key:
                for fallback in ("study title", "study description", "title"):
                    type_key = key_map.get(fallback)
                    if type_key:
                        break
            if not study_key:
                continue
            study_number = str(row.get(study_key) or "").strip()
            if not study_number:
                continue
            study_ids = _extract_study_ids(study_number)
            if not study_ids:
                continue
            study_id = study_ids[0]
            if study_id in seen:
                continue
            candidates.append(
                {
                    "type_of_study": (
                        str(row.get(type_key) or "").strip() if type_key else ""
                    ),
                    "test_system": _pick_first_value(
                        row, key_map, "test system", "species strain"
                    ),
                    "method_of_administration": str(
                        row.get(key_map.get("method of administration") or "") or ""
                    ).strip(),
                    "testing_facility": str(
                        row.get(key_map.get("testing facility") or "") or ""
                    ).strip(),
                    "study_number": study_id,
                }
            )
            seen.add(study_id)
    return candidates


def _repair_overview_table(
    tables: Sequence[Dict[str, Any]],
    context: Dict[str, Any],
) -> None:
    target = next(
        (table for table in tables if str(table.get("subsection") or "") == "2.6.3.1"),
        None,
    )
    if not target:
        return
    columns = [str(col) for col in (target.get("columns") or [])]
    if not columns:
        return
    study_col = next(
        (col for col in columns if _normalize_header_token(col) == "study number"), None
    )
    if not study_col:
        return

    candidates = _extract_overview_candidates(context)
    if not candidates:
        return
    study_id_candidates = _build_study_id_candidates(context)

    def candidate_text(candidate: Dict[str, str]) -> str:
        parts = [
            candidate.get("type_of_study", ""),
            candidate.get("test_system", ""),
            candidate.get("method_of_administration", ""),
            candidate.get("testing_facility", ""),
        ]
        return " ".join(part for part in parts if part)

    candidate_tokens = [(_tokenize_text(candidate_text(c)), c) for c in candidates]
    candidate_index_by_study = {
        candidate.get("study_number", ""): idx
        for idx, candidate in enumerate(candidates)
    }

    matched_indices: set[int] = set()
    rows = target.get("rows") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        current = str(row.get(study_col) or "").strip()
        if current and _extract_study_ids(current):
            if current in candidate_index_by_study:
                matched_indices.add(candidate_index_by_study[current])
            continue
        row_text = " ".join(
            str(row.get(col) or "")
            for col in columns
            if _normalize_header_token(col) not in {"study number", "location in ctd"}
        ).strip()
        row_tokens = _tokenize_text(row_text)
        if not row_tokens:
            continue
        best_score = 0.0
        best_index: Optional[int] = None
        for idx, (tokens, candidate) in enumerate(candidate_tokens):
            if not tokens:
                continue
            overlap = row_tokens & tokens
            score = len(overlap) / max(len(tokens), 1)
            if score > best_score:
                best_score = score
                best_index = idx
        if best_index is not None and best_score >= 0.25:
            row[study_col] = candidates[best_index]["study_number"]
            matched_indices.add(best_index)

    # Append any unmatched extracted rows.
    for idx, candidate in enumerate(candidates):
        if idx in matched_indices:
            continue
        new_row = {col: "" for col in columns}
        for col in columns:
            key = _normalize_header_token(col)
            if key == "type of study":
                new_row[col] = candidate.get("type_of_study", "")
            elif key == "test system":
                new_row[col] = candidate.get("test_system", "")
            elif key == "method of administration":
                new_row[col] = candidate.get("method_of_administration", "")
            elif key == "testing facility":
                new_row[col] = candidate.get("testing_facility", "")
            elif key == "study number":
                new_row[col] = candidate.get("study_number", "")
        rows.append(new_row)

    if study_id_candidates:
        existing_ids = {
            str(row.get(study_col) or "").strip()
            for row in rows
            if isinstance(row, dict)
        }
        for entry in study_id_candidates.values():
            study_id = str(entry.get("study_id") or "").strip()
            if not study_id or study_id in existing_ids:
                continue
            new_row = {col: "" for col in columns}
            for col in columns:
                if _normalize_header_token(col) == "study number":
                    new_row[col] = study_id
            rows.append(new_row)
            existing_ids.add(study_id)

    deduped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        study_id = str(row.get(study_col) or "").strip()
        if not study_id:
            continue
        score = sum(1 for col in columns if str(row.get(col) or "").strip())
        current = deduped.get(study_id)
        if not current or score > current.get("_score", 0):
            row_copy = dict(row)
            row_copy["_score"] = score
            deduped[study_id] = row_copy
    if deduped:
        rows[:] = [
            {k: v for k, v in entry.items() if k != "_score"}
            for entry in deduped.values()
        ]


def _tabulated_template_entries_for_section(section: str) -> List[Dict[str, Any]]:
    entries = _load_ind_template_entries()
    matches: List[Dict[str, Any]] = []
    target = section.strip()
    if not target:
        return matches
    target_lower = target.lower()
    token_match = re.search(r"\d+(?:\.\d+)+", target_lower)
    if token_match:
        target_lower = token_match.group(0)
    if target_lower.endswith(".x") or target_lower.endswith(".*"):
        target_lower = target_lower[:-2]
    target_lower = target_lower.rstrip(".")
    for entry in entries:
        raw = entry.get("raw") or {}
        if not isinstance(raw, dict):
            continue
        if not (raw.get("Table Description Boilerplate") or raw.get("Columns Headers")):
            continue
        sec = str(entry.get("section") or "").strip()
        sub = str(entry.get("subsection") or "").strip()
        if sub and sub.lower() == target_lower:
            matches.append(entry)
            continue
        if sec and sec.lower() == target_lower:
            matches.append(entry)
            continue
        if sec and sec.lower().startswith(target_lower):
            matches.append(entry)
    return matches


def _read_table_json_preview(
    s3_client: Any,
    *,
    bucket: str,
    key: str,
    max_rows: int,
) -> List[Dict[str, Any]]:
    raw = _read_s3_text(s3_client, bucket, key)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        if all(isinstance(row, dict) for row in payload):
            return [row for row in payload[:max_rows] if isinstance(row, dict)]
        if all(isinstance(row, list) for row in payload):
            header_idx: Optional[int] = None
            for idx, row in enumerate(payload):
                if not row:
                    continue
                normalized = [_normalize_header_token(str(cell)) for cell in row]
                hits = sum(
                    1
                    for token in normalized
                    if token
                    in {
                        "study number",
                        "type of study",
                        "test system",
                        "organ systems evaluated",
                        "noteworthy findings",
                    }
                )
                if hits >= 2 or "study number" in normalized:
                    header_idx = idx
                    break
            if header_idx is None:
                return []
            headers = [str(cell).strip() for cell in payload[header_idx]]
            rows: List[Dict[str, Any]] = []
            for row in payload[header_idx + 1 :]:
                if not isinstance(row, list):
                    continue
                row_dict = {
                    headers[i]: row[i] if i < len(row) else ""
                    for i in range(len(headers))
                }
                rows.append(row_dict)
                if len(rows) >= max_rows:
                    break
            return rows
    return []


def _build_tabulated_context(
    db: Session,
    *,
    section: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
    max_table_rows: int = 10,
    max_tables: int = 50,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    if section.startswith("2.6.3"):
        max_table_rows = max(max_table_rows, 50)
    project_name = fetch_project_name(db, project_id)
    project_like = f"%/{project_name}/%" if project_name else "%"

    module4_sections, mapping_entries, targets = module4_sections_for_ctd_targets(
        section
    )
    mapping_filter = "prefix"
    fallback_section: Optional[str] = None
    if section.startswith("2.6."):
        exact_sections = set()
        filtered_mappings: List[Dict[str, Any]] = []
        for entry in mapping_entries:
            matched_targets = [
                target_entry
                for target_entry in entry.get("targets", [])
                if target_entry.get("section") == section
            ]
            if matched_targets:
                filtered = dict(entry)
                filtered["matched_targets"] = matched_targets
                filtered_mappings.append(filtered)
                exact_sections.add(entry.get("module4_section"))
        if filtered_mappings:
            module4_sections = sorted(s for s in exact_sections if s)
            mapping_entries = filtered_mappings
            mapping_filter = "exact"
        elif mapping_entries:
            fallback_section = f"{section}.*"

    sources = fetch_section_sources(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
        module4_sections=module4_sections,
    )
    project_document_keys = fetch_project_document_keys(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
    )
    document_keys = fetch_document_keys_for_sections(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
        module4_sections=module4_sections,
    )
    ncd_study_ids = fetch_study_ids_for_sections(
        db,
        project_id=project_id,
        module4_sections=module4_sections,
    )

    assets = fetch_assets_for_sections(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
        module4_sections=module4_sections,
        asset_type="table",
    )

    s3_client = _boto3_client("s3")
    s3_listing_keys: List[str] = []
    table_assets: List[Dict[str, Any]] = []
    for row in assets[: max(0, max_tables)]:
        extra = _normalize_extra_attributes(row.get("extra_attributes"))
        json_key = extra.get("json_key")
        preview_rows: List[Dict[str, Any]] = []
        if json_key:
            preview_rows = _read_table_json_preview(
                s3_client,
                bucket=row.get("s3_bucket") or bucket,
                key=json_key,
                max_rows=max_table_rows,
            )
        table_assets.append(
            {
                "id": row.get("id"),
                "s3_bucket": row.get("s3_bucket"),
                "s3_key": row.get("s3_key"),
                "json_key": json_key,
                "caption": row.get("caption"),
                "description": row.get("description"),
                "keywords": row.get("keywords") or [],
                "page_number": row.get("page_number"),
                "index_on_page": row.get("index_on_page"),
                "extra_attributes": extra,
                "columns": extra.get("columns") or [],
                "row_count": extra.get("row_count"),
                "preview_rows": preview_rows,
            }
        )

    s3_listing_keys = _list_module4_study_keys(
        s3_client,
        bucket=bucket,
        project_name=project_name,
        module4_sections=module4_sections,
        seeds=[
            *(asset.get("s3_key") for asset in table_assets if asset.get("s3_key")),
            *(source.get("s3_key") for source in sources if source.get("s3_key")),
            *document_keys,
            *project_document_keys,
        ],
    )

    template_entries = _tabulated_template_entries_for_section(section)
    table_specs: List[Dict[str, Any]] = []
    for entry in template_entries:
        raw = entry.get("raw") or {}
        if not isinstance(raw, dict):
            continue
        columns_header = (
            raw.get("Columns Headers")
            or raw.get("Column Header")
            or raw.get("Column Headers")
        )
        table_specs.append(
            {
                "section": entry.get("section"),
                "subsection": entry.get("subsection"),
                "subsection_header": entry.get("subsection_header"),
                "ind_requirement": raw.get("IND Requirement"),
                "table_description": raw.get("Table Description Boilerplate"),
                "row_content": raw.get("Row Content"),
                "columns_header": columns_header,
                "columns": _parse_columns_header(columns_header),
                "column_examples": raw.get("Column Example Values"),
                "column_mapping": raw.get("Column Value in Module 4 Location Mapping"),
            }
        )

    preview_row_total = sum(
        len(asset.get("preview_rows") or []) for asset in table_assets
    )
    table_assets_with_preview = sum(
        1 for asset in table_assets if asset.get("preview_rows")
    )
    table_assets_with_json = sum(1 for asset in table_assets if asset.get("json_key"))
    warnings: List[str] = []
    if not project_name:
        warnings.append("Project name not found; project_like fallback '%' used.")
    if not module4_sections:
        warnings.append(
            "No Module 4 sections matched the CTD target; check mapping or use a subsection."
        )
    if not mapping_entries:
        warnings.append("No CTD mapping entries matched the request.")
    if not sources:
        warnings.append("No section sources found for the matched Module 4 sections.")
    if not assets:
        warnings.append("No table assets found for the matched Module 4 sections.")
    if fallback_section:
        warnings.append(
            f"No mapping for {section}; using {fallback_section} module sections."
        )

    debug = {
        "project_name": project_name,
        "project_like": project_like,
        "mapping_filter": mapping_filter,
        "ctd_targets": targets,
        "module4_sections_count": len(module4_sections),
        "mapping_count": len(mapping_entries),
        "section_sources_count": len(sources),
        "table_assets_count": len(assets),
        "table_assets_included": len(table_assets),
        "table_assets_with_json_key": table_assets_with_json,
        "table_assets_with_preview_rows": table_assets_with_preview,
        "table_preview_row_count": preview_row_total,
        "table_specs_count": len(table_specs),
        "document_keys_count": len(document_keys),
        "s3_listing_keys_count": len(s3_listing_keys),
        "warnings": warnings,
        "table_asset_samples": [
            {
                "id": asset.get("id"),
                "s3_key": asset.get("s3_key"),
                "json_key": asset.get("json_key"),
                "row_count": asset.get("row_count"),
                "preview_rows_count": len(asset.get("preview_rows") or []),
            }
            for asset in table_assets[:3]
        ],
    }

    return {
        "section": section,
        "ctd_targets": targets,
        "module4_sections": module4_sections,
        "mapping": mapping_entries,
        "table_specs": table_specs,
        "table_assets": table_assets,
        "section_sources": _slim_sources([dict(row) for row in sources]),
        "document_keys": document_keys,
        "project_document_keys": project_document_keys,
        "ncd_study_ids": ncd_study_ids,
        "s3_listing_keys": s3_listing_keys,
    }, debug


def _merge_tabulated_tables(
    table_specs: Sequence[Dict[str, Any]],
    tables: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    def _normalize_rows(
        rows: Sequence[Dict[str, Any]] | None, columns: Sequence[str]
    ) -> List[Dict[str, Any]]:
        if not rows:
            return []
        if not columns:
            return [row for row in rows if isinstance(row, dict)]
        normalized: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized.append({col: row.get(col, "") for col in columns})
        return normalized

    tables_by_subsection = {
        str(table.get("subsection") or ""): table
        for table in tables
        if isinstance(table, dict)
    }
    merged: List[Dict[str, Any]] = []
    for spec in table_specs:
        subsection = str(spec.get("subsection") or "")
        existing = tables_by_subsection.get(subsection, {})
        spec_columns = spec.get("columns") or []
        existing_rows = existing.get("rows") or []
        columns = spec_columns or existing.get("columns") or []
        rows = _normalize_rows(existing_rows, columns)
        merged.append(
            {
                "subsection": subsection,
                "subsection_header": spec.get("subsection_header"),
                "description": spec.get("table_description"),
                "columns": columns,
                "rows": rows,
                "notes": existing.get("notes") or "",
            }
        )
    return merged


def _normalize_tabulated_columns(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        parts = re.split(r"[|\n]", value)
        return [_normalize_header_token(part.strip()) for part in parts if part.strip()]
    if isinstance(value, (list, tuple)):
        return [
            _normalize_header_token(str(item)) for item in value if str(item).strip()
        ]
    return []


def _columns_from_rows(rows: Sequence[Dict[str, Any]] | None) -> List[str]:
    if not rows:
        return []
    first = rows[0] if isinstance(rows[0], dict) else {}
    return _normalize_tabulated_columns(list(first.keys()))


def _realign_tabulated_tables_by_columns(
    table_specs: Sequence[Dict[str, Any]],
    tables: Sequence[Dict[str, Any]],
) -> None:
    spec_columns: Dict[str, List[str]] = {}
    for spec in table_specs:
        subsection = str(spec.get("subsection") or "")
        columns = _normalize_tabulated_columns(spec.get("columns") or [])
        if subsection and columns:
            spec_columns[subsection] = columns

    if not spec_columns:
        return

    for table in tables:
        if not isinstance(table, dict):
            continue
        cols = _normalize_tabulated_columns(table.get("columns") or [])
        if not cols:
            cols = _columns_from_rows(table.get("rows"))
        if not cols:
            continue
        cols_set = set(cols)
        best_subsection: Optional[str] = None
        best_score = 0.0
        for subsection, spec_cols in spec_columns.items():
            spec_set = set(spec_cols)
            if not spec_set:
                continue
            overlap = len(cols_set & spec_set)
            score = overlap / max(len(spec_set), 1)
            if score > best_score:
                best_score = score
                best_subsection = subsection

        current_subsection = str(table.get("subsection") or "")
        current_score = 0.0
        if current_subsection in spec_columns:
            current_set = set(spec_columns[current_subsection])
            current_score = len(cols_set & current_set) / max(len(current_set), 1)

        if (
            best_subsection
            and best_score >= 0.6
            and best_score >= max(current_score + 0.15, 0.75)
        ):
            table["subsection"] = best_subsection


def _build_tabulated_prompt(
    *,
    section: str,
    context: Dict[str, Any],
    user_prompt: Optional[str],
    user_comment: Optional[str],
    previous_tables: Optional[Dict[str, Any]],
    previous_embedding: Any,
) -> tuple[str, str]:
    system_prompt = (
        "You are an expert nonclinical regulatory writer. "
        "Generate CTD Module 2.6 tabulated summaries using only the provided data. "
        "Return JSON only."
    )
    instructions = (
        user_prompt.strip()
        if user_prompt
        else (
            "Use the table specs to populate rows from the available table assets and summaries. "
            "If data is missing, leave rows empty and add notes."
        )
    )
    parts = [
        f"CTD Section: {section}",
        f"Instructions: {instructions}",
        "Return JSON with schema: "
        '{"section": "<section>", "tables": [{"subsection": "...", '
        '"subsection_header": "...", "columns": ["..."], "rows": ['
        '{"<col>": "<value>"}], "notes": ""}]}.',
        "Use the table specs' column headers exactly for the columns list and row keys.",
    ]
    if user_comment:
        if previous_tables:
            parts.append(
                f"Previous tables JSON:\n{json.dumps(previous_tables, ensure_ascii=True, indent=2)}"
            )
        embedding_text = _format_embedding_for_prompt(previous_embedding)
        if embedding_text:
            parts.append(f"Previous tables embedding: {embedding_text}")
        parts.append(f"User comment:\n{user_comment}")
        parts.append("Revise the tables to address the comment.")

    context_json = json.dumps(
        _trim_tabulated_context(context), ensure_ascii=True, indent=2, default=str
    )
    parts.append(f"Context data (JSON):\n{context_json}")
    return system_prompt, "\n\n".join(parts)


async def _get_assets_by_type(
    *,
    section: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
    asset_type: str,
    limit: int,
) -> Dict[str, Any]:
    target = section.strip()
    if not target:
        raise HTTPException(status_code=400, detail="section is required")
    if not (target.startswith("2.4") or target.startswith("2.6")):
        raise HTTPException(
            status_code=400, detail="section must start with 2.4 or 2.6"
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


@ncd_router.get("/assets/image")
async def get_assets_images(
    section: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
    limit: int = 500,
) -> Dict[str, Any]:
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
    return await _get_assets_by_type(
        section=section,
        tenant_id=tenant_id,
        project_id=project_id,
        bucket=bucket,
        asset_type="table",
        limit=limit,
    )


@ncd_router.get("/assets/contents")
async def get_assets_contents(
    section: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
    content_type: Optional[str] = None,
    include_assets: bool = True,
) -> Dict[str, Any]:
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
) -> Dict[str, Any]:
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

        system_prompt, user_prompt = _build_section_summary_prompt(
            section=section,
            context=context,
            user_prompt=payload.user_prompt,
            user_comment=payload.user_comment,
            previous_summary=previous_summary,
            previous_embedding=previous_embedding,
        )
        llm = LLMClient()
        try:
            summary_text = llm.generate_text(system_prompt, user_prompt).strip()
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to generate summary: {exc}",
            ) from exc
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
                    user_prompt=payload.user_prompt,
                    user_comment=payload.user_comment,
                    previous_id=previous_summary_id,
                    model_name=llm.model_name,
                    embedding=embedding,
                )
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to store section summary: {exc}",
            ) from exc
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


@ncd_router.post("/assets/summary/approve")
async def approve_ctd_section_summary(
    payload: CTDSectionSummaryApproveRequest,
) -> Dict[str, Any]:
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
) -> Dict[str, Any]:
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
                if payload.user_prompt:
                    notes.append(f"User prompt: {payload.user_prompt}")
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
            system_prompt, user_prompt = _build_tabulated_prompt(
                section=section,
                context=context,
                user_prompt=payload.user_prompt,
                user_comment=payload.user_comment,
                previous_tables=previous_tables,
                previous_embedding=previous_embedding,
            )
            llm = LLMClient()
            llm_model_name = llm.model_name
            try:
                response = llm.extract_json(system_prompt, user_prompt)
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to generate tabulated summary: {exc}",
                ) from exc

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
                    user_prompt=payload.user_prompt,
                    user_comment=payload.user_comment,
                    previous_id=previous_tabulated_id,
                    model_name=llm_model_name,
                    embedding=embedding,
                )
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to store tabulated summary: {exc}",
            ) from exc
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


async def _analyze_uploaded_pdf(upload: UploadFile) -> Dict[str, Any]:
    """Analyze an uploaded PDF file entirely in memory."""
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
    """Download a PDF from S3, run the pipeline, and return the analysis payload."""
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

        if markdown:
            _store_embedding_for_document(
                bucket=payload.bucket,
                key=payload.key,
                version_id=payload.version_id,
                filename=Path(payload.key).name,
                markdown=markdown,
                metadata_fields=metadata_fields,
            )

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
        raise HTTPException(
            status_code=502, detail=f"Failed to download S3 object: {exc}"
        ) from exc


@upload_router.post("/analyze")
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
        try:
            data = await request.json()
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
        raise HTTPException(
            status_code=502, detail=f"Failed to upload to S3: {exc}"
        ) from exc

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
        raise HTTPException(
            status_code=500, detail=f"Failed to schedule analysis: {exc}"
        ) from exc

    def _log_async_failure(fut: asyncio.Future[Any]) -> None:
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
            raise HTTPException(
                status_code=500, detail=f"Analysis failed: {exc}"
            ) from exc

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
        raise HTTPException(
            status_code=502, detail=f"Failed to download S3 object: {exc}"
        ) from exc
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
        raise HTTPException(
            status_code=502, detail=f"Failed to download S3 object: {exc}"
        ) from exc
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
        raise HTTPException(
            status_code=502, detail=f"Failed to upload markdown: {exc}"
        ) from exc


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
        raise HTTPException(
            status_code=502, detail=f"Failed to check analysis status: {exc}"
        ) from exc


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
            raise HTTPException(
                status_code=500,
                detail=f"Stored analysis payload is invalid JSON: {exc}",
            ) from exc

    try:
        return await asyncio.to_thread(worker)
    except HTTPException:
        raise
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - boto specific
        raise HTTPException(
            status_code=502, detail=f"Failed to download analysis result: {exc}"
        ) from exc


app.include_router(upload_router)
app.include_router(ncd_router)
app.include_router(dev_router)


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
