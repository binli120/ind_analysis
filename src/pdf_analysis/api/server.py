# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""FastAPI service that powers PDF analysis, uploads, and downstream tooling."""


from __future__ import annotations

import ast
import asyncio
import json
import logging
import html
import os
import re
import shutil
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, cast
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
from fastapi import (
    APIRouter,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
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
from pdf_analysis.ingest.docx_text import extract_docx_pages
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
try:  # Optional tokenizer for accurate embedding chunking.
    import tiktoken
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    tiktoken = None  # type: ignore[assignment]

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
    load_module4_to_26_mapping,
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


@app.middleware("http")
async def log_unhandled_exceptions(request: Request, call_next):
    try:
        return await call_next(request)
    except HTTPException:
        # Let FastAPI handle expected HTTP errors
        raise
    except Exception:
        logger.exception("Unhandled error for %s %s", request.method, request.url.path)
        raise


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


class S3NewProjectRequest(BaseModel):
    bucket: str
    tenant_name: str
    project_name: str
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
    section_scope: str = "auto"


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


class NCDGapAnalysisRequest(BaseModel):
    """Request payload for project-level gap analysis."""

    project_id: str
    bucket: str
    tenant_id: Optional[str] = None
    project_prefix: Optional[str] = None
    include_optional_p1: bool = False
    aws_region: Optional[str] = None


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
_EMBEDDING_MAX_INPUT_TOKENS = 6000
_EMBEDDING_CHUNK_OVERLAP_TOKENS = 200


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


def _embedding_encoding() -> Any:
    if tiktoken is None:
        return None
    try:
        model_name = settings.embedding_model_name or ""
        return tiktoken.encoding_for_model(model_name)
    except Exception:
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None


def _approx_token_count(text: str) -> int:
    encoding = _embedding_encoding()
    if encoding is not None:
        try:
            return len(encoding.encode(text))
        except Exception:
            pass
    # Fallback approximation when tokenizer is unavailable.
    return max(1, len(text) // 4)


def _split_text_for_embedding(
    text: str,
    *,
    max_tokens: int = _EMBEDDING_MAX_INPUT_TOKENS,
    overlap_tokens: int = _EMBEDDING_CHUNK_OVERLAP_TOKENS,
) -> List[str]:
    normalized = str(text or "").strip()
    if not normalized:
        return []
    if max_tokens <= 0:
        return [normalized]

    encoding = _embedding_encoding()
    if encoding is not None:
        try:
            tokens = encoding.encode(normalized)
        except Exception:
            tokens = []
        if tokens:
            if len(tokens) <= max_tokens:
                return [normalized]
            chunks: List[str] = []
            step = max(1, max_tokens - max(0, overlap_tokens))
            start = 0
            while start < len(tokens):
                end = min(len(tokens), start + max_tokens)
                chunk_tokens = tokens[start:end]
                if not chunk_tokens:
                    break
                chunk_text = encoding.decode(chunk_tokens).strip()
                if chunk_text:
                    chunks.append(chunk_text)
                if end >= len(tokens):
                    break
                start += step
            return chunks

    if _approx_token_count(normalized) <= max_tokens:
        return [normalized]

    max_chars = max(1000, max_tokens * 4)
    chunks: List[str] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + max_chars)
        if end < len(normalized):
            split_at = normalized.rfind(" ", start, end)
            if split_at > start + 200:
                end = split_at
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(normalized):
            break
        overlap_chars = max(100, overlap_tokens * 4)
        start = max(0, end - overlap_chars)
    return chunks


def _weighted_average_embeddings(
    embeddings: Sequence[List[float]], weights: Sequence[float]
) -> Optional[List[float]]:
    if not embeddings or not weights or len(embeddings) != len(weights):
        return None
    total_weight = sum(weight for weight in weights if weight > 0)
    if total_weight <= 0:
        return None
    dim = len(embeddings[0])
    if dim == 0:
        return None
    for emb in embeddings:
        if len(emb) != dim:
            return None
    merged = [0.0] * dim
    for emb, weight in zip(embeddings, weights):
        if weight <= 0:
            continue
        for idx, value in enumerate(emb):
            merged[idx] += float(value) * weight
    return [value / total_weight for value in merged]


def _request_embedding(client: OpenAI, text: str) -> Optional[List[float]]:
    response = client.embeddings.create(
        model=settings.embedding_model_name,
        input=text,
    )
    if not response.data:
        return None
    return list(response.data[0].embedding)


def _generate_embedding(text: str, expected_dim: int = 1536) -> Optional[List[float]]:
    client = _get_embedding_client()
    if not client:
        return None
    text_value = str(text or "").strip()
    if not text_value:
        return None
    chunks = _split_text_for_embedding(text_value)
    if not chunks:
        return None
    chunk_embeddings: List[List[float]] = []
    chunk_weights: List[float] = []
    for chunk in chunks:
        try:
            embedding = _request_embedding(client, chunk)
        except Exception as exc:  # pragma: no cover - network/API failure
            logger.warning("Failed to generate section summary embedding: %s", exc)
            return None
        if not embedding:
            continue
        chunk_embeddings.append(embedding)
        chunk_weights.append(float(_approx_token_count(chunk)))
    if not chunk_embeddings:
        return None
    if len(chunk_embeddings) == 1:
        embedding = chunk_embeddings[0]
    else:
        merged = _weighted_average_embeddings(chunk_embeddings, chunk_weights)
        if not merged:
            return None
        embedding = merged
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


def _normalize_s3_path_segment(value: str, field_name: str) -> str:
    cleaned = value.strip().strip("/")
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    return cleaned


def _s3_folder_template_path() -> Path:
    config_dir = Path(__file__).resolve().parents[3] / "config"
    primary = config_dir / "s3_folder_template.json"
    if primary.exists():
        return primary
    fallback = config_dir / "s3_folder_templates.json"
    if fallback.exists():
        return fallback
    raise FileNotFoundError(
        f"Missing template file: {primary} (fallback checked: {fallback})"
    )


def _load_s3_folder_template() -> Dict[str, Any]:
    template_path = _s3_folder_template_path()
    payload = json.loads(template_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("S3 folder template must be a JSON object")
    return payload


def _collect_s3_folder_keys(base_prefix: str, structure: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    for folder_name, subfolders in structure.items():
        normalized_name = str(folder_name).strip().strip("/")
        if not normalized_name:
            continue
        prefix = f"{base_prefix}{normalized_name}/"
        keys.append(prefix)
        if isinstance(subfolders, dict) and subfolders:
            keys.extend(_collect_s3_folder_keys(prefix, subfolders))
    return keys


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
# Section labeling helpers (IND 2.4/2.6 + CTD section list)
# ---------------------------------------------------------------------------
_IND_TEMPLATE_SECTIONS: List[Dict[str, str]] | None = None
_IND_TEMPLATE_ENTRIES: List[Dict[str, Any]] | None = None
_SECTION_LIST: List[Dict[str, Any]] | None = None
_CTD_SECTION_SECTIONS: List[Dict[str, str]] | None = None


def _section_list_path() -> Path:
    return Path(__file__).resolve().parents[2] / "ncd" / "sectionList.json"


def _load_section_list() -> List[Dict[str, Any]]:
    """Load the hierarchical section list from src/ncd/sectionList.json."""
    global _SECTION_LIST
    if _SECTION_LIST is not None:
        return _SECTION_LIST

    path = _section_list_path()
    if not path.exists():
        raise FileNotFoundError(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("sectionList.json must contain a JSON array")

    _SECTION_LIST = payload
    return _SECTION_LIST


def _reset_ind_template_cache() -> None:
    global _IND_TEMPLATE_SECTIONS, _IND_TEMPLATE_ENTRIES, _SECTION_LIST, _CTD_SECTION_SECTIONS
    _IND_TEMPLATE_SECTIONS = None
    _IND_TEMPLATE_ENTRIES = None
    _SECTION_LIST = None
    _CTD_SECTION_SECTIONS = None


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


def _load_ctd_section_sections() -> List[Dict[str, str]]:
    """Load CTD section numbers/titles from sectionList.json."""
    global _CTD_SECTION_SECTIONS
    if _CTD_SECTION_SECTIONS is not None:
        return _CTD_SECTION_SECTIONS

    try:
        entries = _load_section_list()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Unable to load CTD section list: %s", exc)
        _CTD_SECTION_SECTIONS = []
        return _CTD_SECTION_SECTIONS

    sections: Dict[str, Dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("value") or "").strip()
        if not value or not re.search(r"\d", value):
            continue
        if value.lower().startswith("m"):
            continue
        text = str(entry.get("text") or value).strip()
        text = re.sub(r"^\s+", "", text)
        title = text
        if title.startswith(value):
            title = title[len(value) :].lstrip(" —-")
        if not title:
            title = value
        blob_parts = [title, entry.get("desc") or ""]
        blob = " ".join(part for part in blob_parts if part).strip()
        if value not in sections:
            sections[value] = {"section": value, "title": title, "blob": blob}

    _CTD_SECTION_SECTIONS = list(sections.values())
    return _CTD_SECTION_SECTIONS


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
        # Avoid trivial matches like "1" or "2" that appear in any document.
        if re.fullmatch(r"\d+", sec):
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
        "You are a regulatory assistant classifying CTD/IND documents. "
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
    text: str,
    *,
    filename: Optional[str] = None,
    use_llm: bool = False,
    sections: Optional[List[Dict[str, str]]] = None,
) -> Tuple[str, str, float, str] | None:
    """Determine the best section using filename hints + minimal text."""
    sections = sections or _load_ind_template_sections()
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


def _section_module(section_number: Optional[str]) -> Optional[str]:
    if not section_number:
        return None
    match = re.match(r"^(\d+)", section_number)
    return match.group(1) if match else None


def _select_label_classification(
    *,
    ind_class: Optional[Tuple[str, str, float, str]],
    ctd_class: Optional[Tuple[str, str, float, str]],
    ind_sections: List[Dict[str, str]],
    ctd_sections: List[Dict[str, str]],
) -> Tuple[Optional[Tuple[str, str, float, str]], List[Dict[str, str]]]:
    if ctd_class is None:
        return ind_class, ind_sections
    if ind_class is None:
        return ctd_class, ctd_sections

    ctd_module = _section_module(ctd_class[0])
    if ctd_module and ctd_module != "2":
        return ctd_class, ctd_sections
    if ctd_module == "2":
        return ind_class, ind_sections

    if ctd_class[2] > ind_class[2]:
        return ctd_class, ctd_sections
    return ind_class, ind_sections


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
            scope = (payload.section_scope or "auto").strip().lower()
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
                    filename=Path(payload.key).name,
                    use_llm=payload.use_llm,
                    sections=ind_sections,
                )
            elif scope == "ctd":
                classification = _classify_section_from_text(
                    sample_text,
                    filename=Path(payload.key).name,
                    use_llm=payload.use_llm,
                    sections=ctd_sections,
                )
            else:
                ind_class = _classify_section_from_text(
                    sample_text,
                    filename=Path(payload.key).name,
                    use_llm=payload.use_llm,
                    sections=ind_sections,
                )
                ctd_class = _classify_section_from_text(
                    sample_text,
                    filename=Path(payload.key).name,
                    use_llm=payload.use_llm,
                    sections=ctd_sections,
                )
                classification, _ = _select_label_classification(
                    ind_class=ind_class,
                    ctd_class=ctd_class,
                    ind_sections=ind_sections,
                    ctd_sections=ctd_sections,
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
            candidate_sections = ind_sections
        elif scope == "ctd":
            classification = _classify_section_from_text(
                sample_text,
                filename=filename,
                use_llm=use_llm,
                sections=ctd_sections,
            )
            candidate_sections = ctd_sections
        else:
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
            classification, candidate_sections = _select_label_classification(
                ind_class=ind_class,
                ctd_class=ctd_class,
                ind_sections=ind_sections,
                ctd_sections=ctd_sections,
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
            candidate_sections = ind_sections
        elif scope == "ctd":
            classification = _classify_section_from_text(
                sample_text,
                filename=filename,
                use_llm=use_llm,
                sections=ctd_sections,
            )
            candidate_sections = ctd_sections
        else:
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
            classification, candidate_sections = _select_label_classification(
                ind_class=ind_class,
                ctd_class=ctd_class,
                ind_sections=ind_sections,
                ctd_sections=ctd_sections,
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
        raise HTTPException(status_code=500, detail=str(exc)) from exc

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
        raise HTTPException(
            status_code=500, detail=f"Failed to upsert template override: {exc}"
        ) from exc
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
                raise HTTPException(
                    status_code=500, detail=f"Template file is invalid JSON: {exc}"
                ) from exc
            return {"template": payload, "user_id": normalized_user_id}
    finally:
        db.close()

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
    db = SessionLocal()
    try:
        overrides = _fetch_template_overrides(db, normalized_user_id, target)
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

    return {"section": target, "entries": matches, "user_id": normalized_user_id}


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


@ncd_router.get("/gap-analysis")
async def get_gap_analysis(
    project_id: str,
    bucket: str,
    tenant_id: Optional[str] = None,
    project_prefix: Optional[str] = None,
    include_optional_p1: bool = False,
    aws_region: Optional[str] = None,
) -> Dict[str, Any]:
    _require_uuid(project_id, "project_id")
    if not bucket:
        raise HTTPException(status_code=400, detail="bucket is required")

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


@ncd_router.post("/gap-analysis")
async def post_gap_analysis(payload: NCDGapAnalysisRequest) -> Dict[str, Any]:
    _require_uuid(payload.project_id, "project_id")
    if not payload.bucket:
        raise HTTPException(status_code=400, detail="bucket is required")

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
    section_value = str(context.get("section") or "")

    def _row_key(row: Dict[str, Any]) -> tuple[Any, Any, Any]:
        return (
            row.get("document_section_id") or row.get("section_id"),
            row.get("section_number"),
            row.get("document_version_id"),
        )

    def _select_rows(
        rows: Sequence[Dict[str, Any]],
        *,
        limit: int,
        head_count: int,
        length_field: str,
    ) -> List[Dict[str, Any]]:
        if limit <= 0:
            return []
        valid_rows = [row for row in rows if isinstance(row, dict)]
        if not valid_rows:
            return []

        selected: List[Dict[str, Any]] = []
        seen: Set[tuple[Any, Any, Any]] = set()
        keep_head = min(head_count, limit, len(valid_rows))

        for row in valid_rows[:keep_head]:
            key = _row_key(row)
            if key in seen:
                continue
            seen.add(key)
            selected.append(row)

        remaining = sorted(
            valid_rows[keep_head:],
            key=lambda row: len(str(row.get(length_field) or "")),
            reverse=True,
        )
        for row in remaining:
            if len(selected) >= limit:
                break
            key = _row_key(row)
            if key in seen:
                continue
            seen.add(key)
            selected.append(row)
        return selected

    def _extract_numeric_evidence(
        text: str, *, max_items: int = 3, max_chars: int = 180
    ) -> List[str]:
        if not text:
            return []
        scored: List[tuple[int, str]] = []
        seen: set[str] = set()
        tokens = (
            "mg/kg",
            "q2d",
            "q3d",
            "%",
            "p<",
            "p =",
            "n=",
            "fold",
            "cmax",
            "auc",
            "half-life",
            "half life",
            "terminal",
            "clearance",
            "cl",
            "vss",
            "bioavailability",
            "urine",
            "feces",
            "iv",
            "ip",
            "ic50",
            "ec50",
            "kd",
            "qt",
            "qtc",
            "heart rate",
            "blood pressure",
        )
        for line in re.split(r"[\r\n]+", text):
            for clause in re.split(r"(?<=[.!?;])\s+", line):
                snippet = clause.strip()
                if len(snippet) < 12:
                    continue
                lowered = snippet.lower()
                if not any(ch.isdigit() for ch in snippet):
                    continue
                if not any(token in lowered for token in tokens):
                    continue
                normalized = re.sub(r"\s+", " ", lowered)
                if normalized in seen:
                    continue
                seen.add(normalized)
                score = 0
                if "p<" in lowered or "p =" in lowered:
                    score += 4
                if "%" in lowered:
                    score += 3
                if "mg/kg" in lowered:
                    score += 2
                if "q2d" in lowered or "q3d" in lowered:
                    score += 1
                if "cmax" in lowered or "auc" in lowered:
                    score += 2
                scored.append((score, _truncate_text(snippet, max_chars)))
        scored.sort(key=lambda item: (-item[0], -len(item[1])))
        return [snippet for _, snippet in scored[:max_items]]

    def _coerce_summary_body(raw_value: Any, _depth: int = 0) -> str:
        if _depth > 3:
            return str(raw_value or "").strip()
        if raw_value is None:
            return ""
        if isinstance(raw_value, dict):
            lead = raw_value.get("summary")
            if isinstance(lead, str) and lead.strip():
                return _coerce_summary_body(lead.strip(), _depth + 1)
            return json.dumps(raw_value, ensure_ascii=True)

        text = str(raw_value).strip()
        if not text:
            return ""
        parsed: Any = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            if text.startswith("{") and "summary" in text and "'" in text:
                try:
                    parsed = ast.literal_eval(text)
                except (ValueError, SyntaxError):
                    parsed = None
        if isinstance(parsed, str):
            return _coerce_summary_body(parsed.strip(), _depth + 1)
        if isinstance(parsed, dict):
            lead = parsed.get("summary")
            if isinstance(lead, str) and lead.strip():
                return _coerce_summary_body(lead.strip(), _depth + 1)
            topics = parsed.get("topics")
            if isinstance(topics, list):
                snippets: List[str] = []
                for item in topics[:2]:
                    if not isinstance(item, dict):
                        continue
                    topic_summary = item.get("summary")
                    if isinstance(topic_summary, str) and topic_summary.strip():
                        snippets.append(topic_summary.strip())
                if snippets:
                    return " ".join(snippets)
        return text

    def _trim_sources(
        sources: Sequence[Dict[str, Any]],
        *,
        limit: int,
        max_summary_chars: int,
    ) -> List[Dict[str, Any]]:
        trimmed: List[Dict[str, Any]] = []
        selected_rows = _select_rows(
            sources, limit=limit, head_count=5, length_field="summary_text"
        )
        section_is_262 = section_value.startswith("2.6.2")
        section_is_264 = section_value.startswith("2.6.4")
        required_groups: tuple[tuple[str, ...], ...] = ()
        protected_patterns: set[str] = set()
        # For 2.6.2, preserve key anti-tumor response studies even when the
        # context needs aggressive trimming.
        if section_is_262:
            required_groups = (
                ("response_of_sc_ht29",),
                ("response_of_sc_colo205",),
                ("response_of_sc_du145",),
                (
                    "antitumor_activity_in_the_mcf-7_human_breast_tumor_orthotopic_xenograft_model",
                    "did_not_reduce_the_growth_of_established_murine_lewis_lung_tumors",
                ),
                (
                    "cross-reactivity_of_lt1009_towards_lipids",
                    "kinetics_and_stoichiometry_of_the_interaction_between_lt1009_and_s1p",
                    "the_binding_kinetics_of_lt1009_to_s1p_as_measured_by_biacore",
                    "molecular_modeling_and_mutagenesis_studies_of_the_humanized_monoclonal_antibody_lt1009_binding_to_the_biolipid_sphingosine-1-phosphate",
                ),
                (
                    "lt1002_inhibits_sphingosine_1-phosphate-induced_migration_of_cultured_human_umbilical_vein_endothelial_cells",
                    "pilot_bioassay_of_manufactured_lt1009_and_lt1002_based_on_neutralization_s1p-induced_il-8_release_from_human_ovarian_skov3_tumor_cells",
                ),
                (
                    "cardiovascular_and_respiratory_safety_pharmacology_study_of_lt1009_administered_by_a_30-minute",
                    "telemetered_cynomolgus_monkeys",
                ),
            )
            protected_patterns = {
                pattern for group in required_groups for pattern in group
            }
            source_rows = [row for row in sources if isinstance(row, dict)]
            required_rows: List[Dict[str, Any]] = []
            for pattern_group in required_groups:
                candidate = next(
                    (
                        row
                        for row in source_rows
                        if any(
                            pattern in str(row.get("section_number") or "").lower()
                            for pattern in pattern_group
                        )
                    ),
                    None,
                )
                if not candidate:
                    continue
                if _row_key(candidate) in {_row_key(row) for row in required_rows}:
                    continue
                required_rows.append(candidate)

            # Compose with guaranteed coverage: keep a small number of head rows,
            # then append all required buckets, then fill remaining from ranked rows.
            effective_limit = max(limit, len(required_rows))
            selected_rows = _select_rows(
                sources, limit=effective_limit, head_count=5, length_field="summary_text"
            )
            composed: List[Dict[str, Any]] = []
            composed_keys: set[tuple[Any, Any, Any]] = set()
            required_keys = {_row_key(row) for row in required_rows}
            head_allowance = max(0, effective_limit - len(required_rows))
            for row in selected_rows:
                if len(composed) >= head_allowance:
                    break
                key = _row_key(row)
                if key in required_keys or key in composed_keys:
                    continue
                composed.append(row)
                composed_keys.add(key)
            for row in required_rows:
                if len(composed) >= effective_limit:
                    break
                key = _row_key(row)
                if key in composed_keys:
                    continue
                composed.append(row)
                composed_keys.add(key)
            for row in selected_rows:
                if len(composed) >= effective_limit:
                    break
                key = _row_key(row)
                if key in composed_keys:
                    continue
                composed.append(row)
                composed_keys.add(key)
            selected_rows = composed

        for row in selected_rows:
            row_section = str(row.get("section_number") or "").lower()
            is_protected = section_is_262 and any(
                pattern in row_section for pattern in protected_patterns
            )
            summary_limit = max_summary_chars
            if is_protected:
                summary_limit = max(summary_limit, 320)
            raw_summary = _coerce_summary_body(row.get("summary_text"))
            numeric_evidence = _extract_numeric_evidence(
                raw_summary,
                max_items=6 if section_is_264 else 4,
                max_chars=220 if section_is_264 else 170,
            )
            if numeric_evidence:
                summary_limit = max(summary_limit, 320)
            trimmed.append(
                {
                    "section_number": row.get("section_number"),
                    "section_title": row.get("section_title"),
                    "summary_text": _truncate_text(
                        raw_summary, summary_limit
                    ),
                    "numeric_evidence": numeric_evidence,
                    "keywords": (row.get("keywords") or [])[:12],
                    "summary_type": row.get("summary_type"),
                    "summary_purpose": row.get("summary_purpose"),
                    "document_section_id": row.get("document_section_id")
                    or row.get("section_id"),
                    "document_version_id": row.get("document_version_id"),
                }
            )
        return trimmed

    def _trim_key_sections(
        sections: Sequence[Dict[str, Any]],
        *,
        limit: int,
        max_text_chars: int,
    ) -> List[Dict[str, Any]]:
        trimmed: List[Dict[str, Any]] = []
        for row in _select_rows(sections, limit=limit, head_count=3, length_field="text"):
            trimmed.append(
                {
                    "section_type": row.get("section_type"),
                    "text": _truncate_text(str(row.get("text") or ""), max_text_chars),
                    "page_start": row.get("page_start"),
                    "page_end": row.get("page_end"),
                    "document_version_id": row.get("document_version_id"),
                }
            )
        return trimmed

    def _trim_table_specs(
        entries: Sequence[Dict[str, Any]],
        *,
        limit: int,
        max_columns: int,
    ) -> List[Dict[str, Any]]:
        trimmed: List[Dict[str, Any]] = []
        if limit <= 0:
            return trimmed
        for row in entries:
            if len(trimmed) >= limit:
                break
            if not isinstance(row, dict):
                continue
            trimmed.append(
                {
                    "subsection": row.get("subsection"),
                    "subsection_header": row.get("subsection_header"),
                    "columns": (row.get("columns") or [])[:max_columns],
                }
            )
        return trimmed

    def _trim_table_numeric_evidence(
        assets: Sequence[Dict[str, Any]],
        *,
        asset_limit: int,
        rows_per_asset: int,
        max_items: int,
        max_row_chars: int,
    ) -> List[Dict[str, Any]]:
        if max_items <= 0:
            return []

        evidence: List[Dict[str, Any]] = []
        seen: set[str] = set()
        tokens = (
            "mg/kg",
            "%",
            "cmax",
            "auc",
            "half-life",
            "half life",
            "terminal",
            "vss",
            "cl",
            "clearance",
            "bioavailability",
            "urine",
            "feces",
            "iv",
            "ip",
            "peak concentration",
            "target organ",
        )
        for asset in assets[:asset_limit]:
            if not isinstance(asset, dict):
                continue
            source = (
                asset.get("caption")
                or asset.get("description")
                or asset.get("s3_key")
                or asset.get("json_key")
                or f"table_asset:{asset.get('id')}"
            )
            rows = asset.get("preview_rows") or []
            for row in rows[:rows_per_asset]:
                if not isinstance(row, dict):
                    continue
                parts: List[str] = []
                for key, value in row.items():
                    key_text = str(key).strip()
                    value_text = str(value).strip() if value is not None else ""
                    if not key_text or not value_text:
                        continue
                    parts.append(f"{key_text}: {value_text}")
                if not parts:
                    continue
                row_text = "; ".join(parts)
                lowered = row_text.lower()
                if not any(ch.isdigit() for ch in row_text):
                    continue
                if not any(token in lowered for token in tokens):
                    continue
                normalized = re.sub(r"\s+", " ", lowered)
                if normalized in seen:
                    continue
                seen.add(normalized)
                evidence.append(
                    {
                        "source": _truncate_text(str(source), 140),
                        "row": _truncate_text(row_text, max_row_chars),
                    }
                )
                if len(evidence) >= max_items:
                    return evidence
        return evidence

    def _trim_mapping(entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        trimmed: List[Dict[str, Any]] = []
        for row in entries:
            if not isinstance(row, dict):
                continue
            targets = [
                target.get("section")
                for target in (row.get("targets") or [])
                if isinstance(target, dict) and target.get("section")
            ]
            matched = [
                target.get("section")
                for target in (row.get("matched_targets") or [])
                if isinstance(target, dict) and target.get("section")
            ]
            trimmed.append(
                {
                    "module4_section": row.get("module4_section"),
                    "category": row.get("category"),
                    "priority": row.get("priority"),
                    "target_sections": sorted(set(targets)),
                    "matched_target_sections": sorted(set(matched)),
                }
            )
        return trimmed

    def _trim_template_entries(
        entries: Sequence[Dict[str, Any]],
        *,
        limit: int,
        max_content_chars: int,
    ) -> List[Dict[str, Any]]:
        trimmed: List[Dict[str, Any]] = []
        for row in entries:
            if len(trimmed) >= limit:
                break
            if not isinstance(row, dict):
                continue
            trimmed.append(
                {
                    "section": row.get("section"),
                    "subsection": row.get("subsection"),
                    "element_number": row.get("element_number"),
                    "section_header": row.get("section_header"),
                    "subsection_header": row.get("subsection_header"),
                    "content": _truncate_text(
                        str(row.get("content") or ""), max_content_chars
                    ),
                }
            )
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
            template_payload = row.get("template") or {}
            slim_template: Dict[str, Any] = {}
            if isinstance(template_payload, dict):
                slim_template = {
                    "section": template_payload.get("section"),
                    "subsection": template_payload.get("subsection"),
                    "element_number": template_payload.get("element_number"),
                    "content": _truncate_text(
                        str(template_payload.get("content") or ""), 280
                    ),
                }
            trimmed.append(
                {
                    "element_number": row.get("element_number"),
                    "section_number": row.get("section_number"),
                    "module4_sections": row.get("module4_sections") or [],
                    "template": slim_template,
                    "sources": _trim_sources(
                        row.get("sources") or [],
                        limit=3,
                        max_summary_chars=max_summary_chars,
                    ),
                    "key_sections": _trim_key_sections(
                        row.get("key_sections") or [],
                        limit=3,
                        max_text_chars=max_text_chars,
                    ),
                }
            )
        return trimmed

    def _build_trimmed(
        *,
        source_limit: int,
        key_limit: int,
        element_limit: int,
        summary_chars: int,
        key_chars: int,
    ) -> Dict[str, Any]:
        section_is_pk = section_value.startswith("2.6.4") or section_value.startswith(
            "2.6.5"
        )
        return {
            "section": context.get("section"),
            "ctd_targets": context.get("ctd_targets") or [],
            "module4_sections": (context.get("module4_sections") or [])[:30],
            "mapping": _trim_mapping(context.get("mapping") or []),
            "template_entries": _trim_template_entries(
                context.get("template_entries") or [],
                limit=20,
                max_content_chars=350,
            ),
            "section_sources": _trim_sources(
                context.get("section_sources") or [],
                limit=source_limit,
                max_summary_chars=summary_chars,
            ),
            "section_key_sections": _trim_key_sections(
                context.get("section_key_sections") or [],
                limit=key_limit,
                max_text_chars=key_chars,
            ),
            "table_specs": _trim_table_specs(
                context.get("table_specs") or [],
                limit=10 if section_is_pk else 0,
                max_columns=10,
            ),
            "table_numeric_evidence": _trim_table_numeric_evidence(
                context.get("table_assets") or [],
                asset_limit=8,
                rows_per_asset=6,
                max_items=18 if section_is_pk else 0,
                max_row_chars=280,
            ),
            "elements": _trim_elements(
                context.get("elements") or [],
                limit=element_limit,
                max_summary_chars=summary_chars,
                max_text_chars=key_chars,
            ),
        }

    for source_limit, key_limit, element_limit, summary_chars, key_chars in (
        (24, 8, 6, 360, 220),
        (18, 6, 4, 320, 180),
        (12, 4, 3, 300, 160),
        (8, 2, 2, 260, 120),
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

    return _build_trimmed(
        source_limit=5,
        key_limit=0,
        element_limit=0,
        summary_chars=280,
        key_chars=0,
    )


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


_GAP_FIELD_STOPWORDS: Set[str] = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "if",
    "in",
    "into",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}

_GAP_MODULE_RE = re.compile(r"\bmodule[\s._-]*([1-5])\b", re.IGNORECASE)
_GAP_MODULES = ("1", "2", "3", "4", "5")


def _gap_normalize_project_prefix(prefix: Optional[str]) -> str:
    if not prefix:
        return ""
    cleaned = prefix.strip().lstrip("/")
    if cleaned and not cleaned.endswith("/"):
        cleaned = f"{cleaned}/"
    return cleaned


def _gap_collect_required_fields(entry: Dict[str, Any]) -> List[str]:
    values: List[str] = []

    def _add(value: Any) -> None:
        if not value:
            return
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            return
        text = str(value).strip()
        if not text:
            return
        if not re.search(r"[A-Za-z]", text):
            return
        values.append(text)

    def _visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                _add(str(key).replace("_", " "))
                _visit(item)
            return
        if isinstance(value, list):
            for item in value:
                _visit(item)
            return
        _add(value)

    for field in ("required_content", "required_parameters"):
        field_values = entry.get(field)
        if isinstance(field_values, list):
            for item in field_values:
                _add(item)
    _visit(entry.get("validation_rules") or {})
    _visit(entry.get("extraction_focus") or {})

    deduped: List[str] = []
    seen: Set[str] = set()
    for item in values:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _gap_collect_required_sections(include_optional_p1: bool) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for entry in load_module4_to_26_mapping():
        module4_section = str(entry.get("module4_section") or "").strip()
        if not module4_section:
            continue
        priority = str(entry.get("priority") or "").strip().upper() or "P1"
        if priority == "P2":
            continue
        if priority == "P1" and not include_optional_p1:
            continue
        rows.append(
            {
                "module4_section": module4_section,
                "priority": priority,
                "category": entry.get("category"),
                "required_fields": _gap_collect_required_fields(entry),
                "mapping": entry,
            }
        )
    rows.sort(key=lambda row: row["module4_section"])
    return rows


def _gap_key_mentions_module4_section(key: str, module4_section: str) -> bool:
    key_tokens = re.findall(r"\d+(?:\.\d+)+|\d{4,}", key or "")
    target = module4_section.strip()
    if not target:
        return False
    collapsed_target = target.replace(".", "")
    for token in key_tokens:
        if token == target or token.startswith(f"{target}."):
            return True
        if token.isdigit() and (
            token == collapsed_target or token.startswith(collapsed_target)
        ):
            return True
    return False


def _gap_section_matches(value: Optional[str], module4_section: str) -> bool:
    section = (value or "").strip()
    if not section:
        return False
    return section_number_matches(section, module4_section)


def _gap_stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=True, default=str)
    except Exception:
        return str(value)


def _gap_tokenize(value: str) -> List[str]:
    return [token for token in re.findall(r"[a-z0-9]+", value.lower()) if token]


def _gap_field_is_present(field: str, corpus: str) -> bool:
    field_text = field.strip().lower()
    corpus_text = (corpus or "").lower()
    if not field_text or not corpus_text:
        return False
    if field_text in corpus_text:
        return True
    if fuzz.partial_ratio(field_text, corpus_text) >= 88:
        return True
    tokens = [
        token
        for token in _gap_tokenize(field_text)
        if token not in _GAP_FIELD_STOPWORDS and len(token) > 1
    ]
    if not tokens:
        return False
    hits = sum(1 for token in tokens if token in corpus_text)
    threshold = max(1, int(len(tokens) * 0.6))
    return hits >= threshold


def _gap_parse_module_number(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip()
    if not text:
        return None
    if text in _GAP_MODULES:
        return text
    match = _GAP_MODULE_RE.search(text)
    if match:
        return match.group(1)
    return None


def _gap_project_filter_sql(
    *,
    project_name: Optional[str],
    project_prefix: Optional[str],
    params: Dict[str, Any],
) -> str:
    normalized_prefix = _gap_normalize_project_prefix(project_prefix)
    if normalized_prefix:
        params["project_prefix_like"] = f"{normalized_prefix}%"
        return "AND dv.s3_key ILIKE :project_prefix_like"
    if project_name:
        params["project_name_like"] = f"%/{project_name}/%"
        return "AND dv.s3_key ILIKE :project_name_like"
    return ""


def _gap_section_filter_sql(
    module4_sections: Sequence[str], params: Dict[str, Any]
) -> str:
    if not module4_sections:
        return ""
    predicates: List[str] = []
    for idx, section in enumerate(module4_sections):
        key = f"gap_mod_{idx}"
        params[key] = section
        params[f"{key}_dot"] = f"{section}.%"
        params[f"{key}_pipe"] = f"%|{section}.%"
        predicates.append(
            f"(ds.section_number = :{key} OR ds.section_number LIKE :{key}_dot "
            f"OR ds.section_number LIKE :{key}_pipe)"
        )
    return "AND (" + " OR ".join(predicates) + ")"


def _gap_fetch_project_document_rows(
    db: Session,
    *,
    bucket: str,
    tenant_id: Optional[str],
    project_name: Optional[str],
    project_prefix: Optional[str],
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"bucket": bucket, "tenant_id": tenant_id}
    project_filter = _gap_project_filter_sql(
        project_name=project_name,
        project_prefix=project_prefix,
        params=params,
    )
    rows = (
        db.execute(
            sqltext(
                f"""
                SELECT DISTINCT
                    dv.id AS document_version_id,
                    d.id AS document_id,
                    d.tenant_id::text AS tenant_id,
                    dv.s3_bucket,
                    dv.s3_key,
                    dv.created_at
                FROM document_versions dv
                JOIN documents d ON d.id = dv.document_id
                WHERE dv.s3_bucket = :bucket
                  AND (:tenant_id IS NULL OR d.tenant_id::text = :tenant_id)
                  {project_filter}
                ORDER BY dv.s3_key
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _gap_fetch_section_rows(
    db: Session,
    *,
    bucket: str,
    tenant_id: Optional[str],
    project_name: Optional[str],
    project_prefix: Optional[str],
    module4_sections: Sequence[str],
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"bucket": bucket, "tenant_id": tenant_id}
    project_filter = _gap_project_filter_sql(
        project_name=project_name,
        project_prefix=project_prefix,
        params=params,
    )
    section_filter = _gap_section_filter_sql(module4_sections, params)
    rows = (
        db.execute(
            sqltext(
                f"""
                SELECT
                    ds.id AS section_id,
                    ds.section_number,
                    ds.section_title,
                    ds.page_start,
                    ds.page_end,
                    ds.char_start,
                    ds.char_end,
                    d.tenant_id::text AS tenant_id,
                    dv.id AS document_version_id,
                    dv.s3_bucket,
                    dv.s3_key,
                    s.summary_text,
                    s.keywords
                FROM document_sections ds
                JOIN document_versions dv ON dv.id = ds.document_version_id
                JOIN documents d ON d.id = dv.document_id
                LEFT JOIN document_section_summary s ON s.section_id = ds.id
                WHERE dv.s3_bucket = :bucket
                  AND (:tenant_id IS NULL OR d.tenant_id::text = :tenant_id)
                  {project_filter}
                  {section_filter}
                ORDER BY dv.s3_key, ds.section_number
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _gap_fetch_study_rows(db: Session, *, project_id: str) -> List[Dict[str, Any]]:
    rows = (
        db.execute(
            sqltext(
                """
                SELECT id, sponsor_study_id, study_type, glp_status, species, route,
                       duration_days, module4_section, extra_attributes, created_at
                FROM ncd_study
                WHERE project_id = :project_id
                ORDER BY created_at DESC NULLS LAST
                """
            ),
            {"project_id": project_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _gap_fetch_source_document_rows(
    db: Session, *, project_id: str
) -> List[Dict[str, Any]]:
    rows = (
        db.execute(
            sqltext(
                """
                SELECT id, file_name, module, ctd_section, uploaded_at
                FROM ncd_source_document
                WHERE project_id = :project_id
                ORDER BY uploaded_at DESC NULLS LAST
                """
            ),
            {"project_id": project_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _gap_infer_tenant_id(
    db: Session, *, project_id: str, explicit_tenant_id: Optional[str]
) -> Optional[str]:
    if explicit_tenant_id:
        return explicit_tenant_id
    try:
        tenant = db.execute(
            sqltext("SELECT tenant_id::text FROM projects WHERE id = :project_id LIMIT 1"),
            {"project_id": project_id},
        ).scalar()
    except Exception:
        return None
    return str(tenant) if tenant else None


def _gap_check_project_prefix(
    *,
    bucket: str,
    project_name: Optional[str],
    project_prefix: Optional[str],
    aws_region: Optional[str],
) -> Dict[str, Any]:
    requested_prefix = _gap_normalize_project_prefix(project_prefix)
    if requested_prefix:
        candidates = [requested_prefix]
    elif project_name:
        candidates = [
            _gap_normalize_project_prefix(project_name),
            _gap_normalize_project_prefix(f"filynai.com/{project_name}"),
        ]
    else:
        candidates = []

    if not candidates:
        return {
            "checked": False,
            "reason": "project_prefix_not_provided",
            "prefix": None,
            "object_count": None,
        }

    try:
        s3_client = _boto3_client("s3", region_name=aws_region)
    except Exception as exc:
        return {
            "checked": False,
            "reason": f"s3_client_unavailable: {exc}",
            "prefix": candidates[0],
            "object_count": None,
        }

    for candidate in candidates:
        try:
            result = s3_client.list_objects_v2(
                Bucket=bucket,
                Prefix=candidate,
                MaxKeys=50,
            )
        except Exception as exc:
            return {
                "checked": False,
                "reason": f"s3_list_failed: {exc}",
                "prefix": candidate,
                "object_count": None,
            }
        count = len(result.get("Contents") or [])
        if count:
            return {
                "checked": True,
                "reason": None,
                "prefix": candidate,
                "object_count": count,
            }

    return {
        "checked": True,
        "reason": None,
        "prefix": candidates[0],
        "object_count": 0,
    }


def _build_gap_analysis_report(
    db: Session,
    *,
    project_id: str,
    bucket: str,
    tenant_id: Optional[str],
    project_prefix: Optional[str],
    include_optional_p1: bool,
    aws_region: Optional[str],
) -> Dict[str, Any]:
    project_name = fetch_project_name(db, project_id)
    effective_tenant = _gap_infer_tenant_id(
        db, project_id=project_id, explicit_tenant_id=tenant_id
    )

    required_sections = _gap_collect_required_sections(include_optional_p1)
    required_module4_sections = [
        str(item["module4_section"]) for item in required_sections
    ]

    diagnostics: List[str] = []
    try:
        document_rows = _gap_fetch_project_document_rows(
            db,
            bucket=bucket,
            tenant_id=effective_tenant,
            project_name=project_name,
            project_prefix=project_prefix,
        )
    except Exception as exc:
        diagnostics.append(f"document_versions query failed: {exc}")
        document_rows = []

    try:
        section_rows = _gap_fetch_section_rows(
            db,
            bucket=bucket,
            tenant_id=effective_tenant,
            project_name=project_name,
            project_prefix=project_prefix,
            module4_sections=required_module4_sections,
        )
    except Exception as exc:
        diagnostics.append(f"document_sections query failed: {exc}")
        section_rows = []

    try:
        study_rows = _gap_fetch_study_rows(db, project_id=project_id)
    except Exception as exc:
        diagnostics.append(f"ncd_study query failed: {exc}")
        study_rows = []

    try:
        source_document_rows = _gap_fetch_source_document_rows(db, project_id=project_id)
    except Exception as exc:
        diagnostics.append(f"ncd_source_document query failed: {exc}")
        source_document_rows = []

    keys = [str(row.get("s3_key") or "") for row in document_rows if row.get("s3_key")]
    prefix_check = _gap_check_project_prefix(
        bucket=bucket,
        project_name=project_name,
        project_prefix=project_prefix,
        aws_region=aws_region,
    )

    missing_files: List[Dict[str, Any]] = []
    missing_data_fields: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    section_reports: List[Dict[str, Any]] = []
    observed_sections: Set[str] = set()
    module_file_counts: Dict[str, int] = {module: 0 for module in _GAP_MODULES}
    module_extracted_counts: Dict[str, int] = {module: 0 for module in _GAP_MODULES}
    module_sample_keys: Dict[str, List[str]] = {module: [] for module in _GAP_MODULES}

    for row in document_rows:
        key = str(row.get("s3_key") or "")
        module = _gap_parse_module_number(key)
        if not module:
            continue
        module_file_counts[module] += 1
        if key and len(module_sample_keys[module]) < 5:
            module_sample_keys[module].append(key)

    for row in source_document_rows:
        module = _gap_parse_module_number(str(row.get("module") or ""))
        if not module:
            module = _gap_parse_module_number(str(row.get("file_name") or ""))
        if not module:
            continue
        module_file_counts[module] += 1
        file_name = str(row.get("file_name") or "")
        if file_name and len(module_sample_keys[module]) < 5:
            module_sample_keys[module].append(file_name)

    for row in section_rows:
        module = _gap_parse_module_number(str(row.get("section_number") or ""))
        if not module:
            continue
        module_extracted_counts[module] += 1

    for row in study_rows:
        module = _gap_parse_module_number(str(row.get("module4_section") or ""))
        if not module:
            continue
        module_extracted_counts[module] += 1

    for required in required_sections:
        module4_section = str(required["module4_section"])
        priority = str(required["priority"])
        required_fields = list(required.get("required_fields") or [])

        matching_key_rows = [
            row
            for row in document_rows
            if _gap_key_mentions_module4_section(str(row.get("s3_key") or ""), module4_section)
        ]
        matching_sources = [
            row
            for row in section_rows
            if _gap_section_matches(str(row.get("section_number") or ""), module4_section)
        ]
        matching_studies = [
            row
            for row in study_rows
            if _gap_section_matches(str(row.get("module4_section") or ""), module4_section)
        ]
        matching_source_docs = [
            row
            for row in source_document_rows
            if _gap_key_mentions_module4_section(str(row.get("file_name") or ""), module4_section)
            or _gap_section_matches(str(row.get("ctd_section") or ""), module4_section)
        ]

        has_files = bool(matching_key_rows or matching_source_docs)
        has_extracted = bool(matching_sources or matching_studies)
        if has_files or has_extracted:
            observed_sections.add(module4_section)

        section_issue_refs: List[str] = []
        if not has_files and not has_extracted:
            issue = {
                "type": "missing_module4_files",
                "severity": "critical" if priority == "P0" else "warning",
                "module4_section": module4_section,
                "priority": priority,
                "message": (
                    f"No files or extracted records found for required Module 4 "
                    f"section {module4_section}."
                ),
            }
            issues.append(issue)
            section_issue_refs.append(issue["type"])
            missing_file_payload = {
                "scope": "module4_section",
                "module4_section": module4_section,
                "priority": priority,
                "category": required.get("category"),
                "reason": "No matching files or extracted records",
            }
            missing_files.append(missing_file_payload)
        elif has_files and not has_extracted:
            issue = {
                "type": "uploaded_not_extracted",
                "severity": "warning",
                "module4_section": module4_section,
                "priority": priority,
                "message": (
                    f"Files detected for {module4_section}, but extracted section/study "
                    "records are missing."
                ),
            }
            issues.append(issue)
            section_issue_refs.append(issue["type"])

        corpus_parts: List[str] = []
        for row in matching_key_rows:
            corpus_parts.append(str(row.get("s3_key") or ""))
        for row in matching_source_docs:
            corpus_parts.append(str(row.get("file_name") or ""))
            corpus_parts.append(str(row.get("ctd_section") or ""))
        for row in matching_sources:
            corpus_parts.append(str(row.get("section_number") or ""))
            corpus_parts.append(str(row.get("section_title") or ""))
            corpus_parts.append(_gap_stringify(row.get("summary_text")))
            corpus_parts.append(_gap_stringify(row.get("keywords")))
        for row in matching_studies:
            corpus_parts.append(str(row.get("sponsor_study_id") or ""))
            corpus_parts.append(str(row.get("study_type") or ""))
            corpus_parts.append(str(row.get("species") or ""))
            corpus_parts.append(str(row.get("route") or ""))
            corpus_parts.append(str(row.get("glp_status") or ""))
            corpus_parts.append(_gap_stringify(row.get("extra_attributes")))
        section_corpus = " ".join(part for part in corpus_parts if part).strip()[:120000]

        missing_fields_for_section: List[str] = []
        if has_files or has_extracted:
            for field_name in required_fields:
                if not _gap_field_is_present(field_name, section_corpus):
                    missing_fields_for_section.append(field_name)

        if missing_fields_for_section:
            issue = {
                "type": "missing_required_data_fields",
                "severity": "critical" if priority == "P0" else "warning",
                "module4_section": module4_section,
                "priority": priority,
                "missing_count": len(missing_fields_for_section),
                "message": (
                    f"Missing {len(missing_fields_for_section)} required field(s) in "
                    f"content linked to {module4_section}."
                ),
                "fields": missing_fields_for_section,
            }
            issues.append(issue)
            section_issue_refs.append(issue["type"])
            missing_data_fields.append(
                {
                    "module4_section": module4_section,
                    "priority": priority,
                    "fields": missing_fields_for_section,
                }
            )

        section_reports.append(
            {
                "module4_section": module4_section,
                "priority": priority,
                "category": required.get("category"),
                "required_fields_count": len(required_fields),
                "missing_fields_count": len(missing_fields_for_section),
                "missing_fields": missing_fields_for_section,
                "file_evidence": {
                    "document_keys": [row.get("s3_key") for row in matching_key_rows],
                    "source_documents": [
                        row.get("file_name") for row in matching_source_docs
                    ],
                    "section_sources": len(matching_sources),
                    "studies": len(matching_studies),
                },
                "status": (
                    "missing_files"
                    if not has_files and not has_extracted
                    else "gaps_found"
                    if missing_fields_for_section
                    else "ok"
                ),
                "issue_types": section_issue_refs,
            }
        )

    module_coverage: List[Dict[str, Any]] = []
    missing_modules: List[str] = []
    for module in _GAP_MODULES:
        file_count = module_file_counts[module]
        extracted_count = module_extracted_counts[module]
        has_files = file_count > 0
        has_extracted = extracted_count > 0
        status = (
            "missing_files"
            if not has_files
            else "uploaded_not_extracted"
            if has_files and not has_extracted
            else "ok"
        )
        module_coverage.append(
            {
                "module": module,
                "has_files": has_files,
                "file_count": file_count,
                "has_extracted_data": has_extracted,
                "extracted_record_count": extracted_count,
                "sample_keys": module_sample_keys[module],
                "status": status,
            }
        )
        if not has_files:
            missing_modules.append(module)
            issues.append(
                {
                    "type": "missing_module_files",
                    "severity": "critical",
                    "module": module,
                    "module4_section": None,
                    "priority": "P0",
                    "message": f"No files detected for Module {module}.",
                }
            )
            missing_files.append(
                {
                    "scope": "module",
                    "module": module,
                    "priority": "P0",
                    "reason": f"Module {module} has no files in project scope",
                }
            )

    if (
        prefix_check.get("checked")
        and prefix_check.get("object_count") == 0
        and not keys
        and not source_document_rows
    ):
        issues.append(
            {
                "type": "project_folder_empty",
                "severity": "critical",
                "module4_section": None,
                "priority": "P0",
                "message": (
                    f"No files found under s3://{bucket}/{prefix_check.get('prefix')}"
                ),
            }
        )

    if not prefix_check.get("checked") and prefix_check.get("reason"):
        diagnostics.append(str(prefix_check.get("reason")))

    critical_count = sum(1 for issue in issues if issue.get("severity") == "critical")
    warning_count = sum(1 for issue in issues if issue.get("severity") == "warning")
    missing_module4_sections_count = sum(
        1 for row in missing_files if row.get("scope") == "module4_section"
    )
    return {
        "project_id": project_id,
        "bucket": bucket,
        "tenant_id": effective_tenant,
        "project_name": project_name,
        "project_prefix": _gap_normalize_project_prefix(project_prefix)
        or prefix_check.get("prefix"),
        "prefix_check": prefix_check,
        "required_module4_sections": required_module4_sections,
        "observed_module4_sections": sorted(observed_sections),
        "module_coverage": module_coverage,
        "missing_modules": missing_modules,
        "missing_files": missing_files,
        "missing_data_fields": missing_data_fields,
        "section_reports": section_reports,
        "issues": issues,
        "summary": {
            "required_modules": len(_GAP_MODULES),
            "missing_modules": len(missing_modules),
            "required_sections": len(required_sections),
            "observed_sections": len(observed_sections),
            "missing_files_total": len(missing_files),
            "missing_file_sections": missing_module4_sections_count,
            "sections_with_missing_fields": len(missing_data_fields),
            "issue_count": len(issues),
            "critical_issues": critical_count,
            "warning_issues": warning_count,
        },
        "diagnostics": diagnostics,
    }


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
    target = normalize_element_number(section)
    if not target:
        return []

    elements: List[str] = []
    seen: Set[str] = set()
    for entry in _load_ind_template_entries():
        raw = entry.get("element_number")
        normalized = normalize_element_number(raw)
        if not normalized or normalized in seen:
            continue
        if (
            normalized == target
            or normalized.startswith(f"{target}.")
            or normalized.startswith(f"{target}-")
        ):
            elements.append(normalized)
            seen.add(normalized)
    return elements


def _filter_mapping_entries_by_module4_sections(
    entries: Sequence[Dict[str, Any]],
    module4_sections: Sequence[str],
) -> List[Dict[str, Any]]:
    allowed = {str(section).strip() for section in module4_sections if str(section).strip()}
    if not allowed:
        return []
    filtered: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        module4_section = str(entry.get("module4_section") or "").strip()
        if module4_section in allowed:
            filtered.append(entry)
    return filtered


def _sections_with_material_data(
    module4_sections: Sequence[str],
    *,
    section_sources: Sequence[Dict[str, Any]],
    document_keys: Sequence[str],
    ncd_payload: Dict[str, Any],
) -> List[str]:
    studies = ncd_payload.get("studies") if isinstance(ncd_payload, dict) else []
    source_documents = (
        ncd_payload.get("source_documents") if isinstance(ncd_payload, dict) else []
    )
    resolved: List[str] = []
    for module4_section in module4_sections:
        section_token = str(module4_section or "").strip()
        if not section_token:
            continue
        has_section_source = any(
            section_number_matches(str(row.get("section_number") or ""), section_token)
            for row in section_sources
            if isinstance(row, dict)
        )
        has_document = any(
            _gap_key_mentions_module4_section(str(key or ""), section_token)
            for key in document_keys
        )
        has_study = any(
            isinstance(row, dict)
            and section_number_matches(
                str(row.get("module4_section") or ""), section_token
            )
            for row in (studies or [])
        )
        has_source_document = any(
            isinstance(row, dict)
            and (
                _gap_key_mentions_module4_section(
                    str(row.get("file_name") or ""), section_token
                )
                or section_number_matches(
                    str(row.get("ctd_section") or ""), section_token
                )
            )
            for row in (source_documents or [])
        )
        if has_section_source or has_document or has_study or has_source_document:
            resolved.append(section_token)
    return sorted(set(resolved))


def _mapped_target_sections_for_request(
    *,
    section: str,
    mapping_entries: Sequence[Dict[str, Any]],
) -> Set[str]:
    target = str(section or "").strip()
    if not target:
        return set()
    matched: Set[str] = set()
    for entry in mapping_entries:
        if not isinstance(entry, dict):
            continue
        target_rows = entry.get("matched_targets") or entry.get("targets") or []
        for row in target_rows:
            if not isinstance(row, dict):
                continue
            target_section = str(row.get("section") or "").strip()
            if not target_section:
                continue
            if target_section == target or target_section.startswith(f"{target}."):
                matched.add(target_section)
    return matched


def _filter_section_entries_for_targets(
    entries: Sequence[Dict[str, Any]],
    *,
    target_sections: Set[str],
) -> List[Dict[str, Any]]:
    if not target_sections:
        return []
    filtered: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        subsection = str(entry.get("subsection") or "").strip()
        section = str(entry.get("section") or "").strip()
        if subsection and subsection in target_sections:
            filtered.append(entry)
            continue
        if not subsection and section and section in target_sections:
            filtered.append(entry)
    return filtered


def _filter_element_numbers_for_targets(
    element_numbers: Sequence[str],
    *,
    target_sections: Set[str],
) -> List[str]:
    if not target_sections:
        return []
    filtered: List[str] = []
    for element in element_numbers:
        token = str(element or "").strip()
        if not token:
            continue
        if any(
            token == target
            or token.startswith(f"{target}.")
            or token.startswith(f"{target}-")
            for target in target_sections
        ):
            filtered.append(token)
    return filtered


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
    ncd_payload: Dict[str, Any] = {}
    try:
        ncd_payload = fetch_ncd_payload(
            db,
            project_id=project_id,
            module4_sections=module4_sections,
        )
    except Exception as exc:
        logger.warning(
            "section summary ctx: failed to fetch ncd payload for section=%s project=%s: %s",
            section,
            project_id,
            exc,
        )
        ncd_payload = {}

    available_sections = _sections_with_material_data(
        module4_sections,
        section_sources=sources,
        document_keys=document_keys,
        ncd_payload=ncd_payload,
    )
    if set(available_sections) != set(module4_sections):
        module4_sections = available_sections
        mapping_entries = _filter_mapping_entries_by_module4_sections(
            mapping_entries,
            module4_sections,
        )
        sources = fetch_section_sources(
            db,
            tenant_id=tenant_id,
            bucket=bucket,
            project_like=project_like,
            module4_sections=module4_sections,
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

    mapped_target_sections = _mapped_target_sections_for_request(
        section=section,
        mapping_entries=mapping_entries,
    )
    template_entries = _template_entries_for_section(section)
    element_numbers = _element_entries_for_section(section)
    if section.startswith("2.6."):
        template_entries = _filter_section_entries_for_targets(
            template_entries,
            target_sections=mapped_target_sections,
        )
        element_numbers = _filter_element_numbers_for_targets(
            element_numbers,
            target_sections=mapped_target_sections,
        )

    table_specs: List[Dict[str, Any]] = []
    table_assets: List[Dict[str, Any]] = []
    if section.startswith("2.6.4") or section.startswith("2.6.5"):
        try:
            tabulated_context, _ = _build_tabulated_context(
                db,
                section=section,
                tenant_id=tenant_id,
                project_id=project_id,
                bucket=bucket,
                max_table_rows=8,
                max_tables=16,
            )
            table_specs = tabulated_context.get("table_specs") or []
            table_assets = tabulated_context.get("table_assets") or []
        except Exception as exc:
            logger.warning(
                "section summary ctx: failed to load tabulated context for section=%s project=%s: %s",
                section,
                project_id,
                exc,
            )

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
        "template_entries": template_entries,
        "section_sources": _slim_sources([dict(row) for row in sources]),
        "section_key_sections": _slim_key_sections([dict(row) for row in key_sections]),
        "table_specs": table_specs,
        "table_assets": table_assets,
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
    section_is_pk = section.startswith("2.6.4") or section.startswith("2.6.5")
    system_prompt = (
        "You are an expert nonclinical regulatory writer. "
        "Generate CTD Module 2.4/2.6 section summaries using only the provided data. "
        "Do not invent data. If key data is missing, state what is missing. "
        "If numeric outcomes are present (dose levels, fold changes, % inhibition, p-values, Cmax/AUC), include them. "
        "For PK sections (2.6.4/2.6.5), report available numeric PK values by study when present "
        "(for example half-life, Cmax, AUC, CL, Vss, bioavailability, urinary/fecal recovery). "
        "Do not claim that data is missing when it appears in the provided context. "
        "Only state that a parameter is unavailable when no corresponding value appears anywhere in the provided context. "
        "Preserve mixed or negative findings and explicitly label non-significant outcomes when reported. "
        "Use exact numbers from the source text only; do not round, average, or merge non-equivalent values. "
        "If studies report different values, present them separately by study and note the discrepancy. "
        "When section_sources include numeric_evidence, prioritize those numbers over paraphrased narrative text. "
        "When table_numeric_evidence is present, treat it as source-of-truth numeric evidence and use it directly with units. "
        "Do not state a PK parameter is unavailable when a value for that parameter appears in table_numeric_evidence. "
        "Write in concise CTD dossier prose (paragraphs with clear study-result statements), avoiding unnecessary outline labels."
    )
    if section_is_pk:
        system_prompt += (
            " For PK sections (2.6.4/2.6.5), preserve subsection structure and present study-by-study numeric findings "
            "for dose, route, half-life, Cmax, AUC, CL, Vss, and bioavailability whenever present."
        )
    instructions = (
        user_prompt.strip()
        if user_prompt
        else (
            "Use the template guidance and data to produce a concise section summary."
            if not section_is_pk
            else (
                "Use the template guidance and data to produce a subsection-aligned PK summary that stays close "
                "to the source table wording and numeric values."
            )
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
    section = str(context.get("section") or "")
    if section.startswith("2.6.3"):
        max_assets = 24
        max_preview_rows = 8
    else:
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
    trimmed_sources: List[Dict[str, Any]] = []
    for source in (context.get("section_sources") or [])[:30]:
        if not isinstance(source, dict):
            continue
        trimmed_sources.append(
            {
                "section_number": source.get("section_number"),
                "section_title": source.get("section_title"),
                "summary_text": _truncate_text(
                    str(source.get("summary_text") or ""), max_chars=1200
                ),
                "keywords": (source.get("keywords") or [])[:15],
            }
        )
    payload = context.get("ncd_payload") or {}
    ncd_studies = []
    if isinstance(payload, dict):
        for study in (payload.get("studies") or [])[:120]:
            if not isinstance(study, dict):
                continue
            extra = _normalize_extra_attributes(study.get("extra_attributes"))
            ncd_studies.append(
                {
                    "sponsor_study_id": study.get("sponsor_study_id"),
                    "study_type": study.get("study_type"),
                    "glp_status": study.get("glp_status"),
                    "species": study.get("species"),
                    "strain": study.get("strain"),
                    "route": study.get("route"),
                    "module4_section": study.get("module4_section"),
                    "extra_attributes": {
                        "type_of_study": extra.get("type_of_study"),
                        "study_title": extra.get("study_title"),
                        "test_system": extra.get("test_system"),
                        "species_strain": extra.get("species_strain"),
                        "method_of_administration": extra.get(
                            "method_of_administration"
                        ),
                        "dose": extra.get("dose"),
                        "dose_level": extra.get("dose_level"),
                        "dose_levels": extra.get("dose_levels"),
                        "endpoints": extra.get("endpoints"),
                        "assays": extra.get("assays"),
                        "key_findings": extra.get("key_findings"),
                        "noteworthy_findings": extra.get("noteworthy_findings"),
                        "organ_systems": extra.get("organ_systems"),
                        "testing_facility": extra.get("testing_facility"),
                        "location_in_ctd": extra.get("location_in_ctd"),
                    },
                }
            )
    return {
        "section": context.get("section"),
        "ctd_targets": context.get("ctd_targets") or [],
        "module4_sections": (context.get("module4_sections") or [])[:20],
        "table_specs": context.get("table_specs") or [],
        "table_assets": trimmed_assets,
        "section_sources": trimmed_sources,
        "ncd_studies": ncd_studies,
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
        if sum(1 for ch in cleaned if ch.isdigit()) < 2:
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


def _canonical_study_number(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = _extract_study_ids(raw)
    if parsed:
        return parsed[0]
    if STUDY_ID_SKIP_RE.match(raw):
        return ""
    # Keep sponsor IDs that don't match strict regex (e.g., "WKP00013 Page 2"),
    # but avoid generic labels and weak fragments.
    digit_count = sum(1 for ch in raw if ch.isdigit())
    alpha_count = sum(1 for ch in raw if ch.isalpha())
    if digit_count >= 2 and alpha_count >= 2 and len(raw) >= 6:
        return raw
    return ""


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


def _study_sort_key(study_number: str) -> Tuple[Any, ...]:
    tokens = re.split(r"(\d+)", (study_number or "").upper())
    key: List[Tuple[int, Any]] = []
    for token in tokens:
        if not token:
            continue
        if token.isdigit():
            key.append((0, int(token)))
        else:
            key.append((1, token))
    return tuple(key)


def _extract_ncd_study_records(
    context: Dict[str, Any],
    *,
    module4_section: Optional[str] = None,
) -> List[Dict[str, Any]]:
    payload = context.get("ncd_payload") or {}
    if not isinstance(payload, dict):
        return []

    studies = payload.get("studies") or []
    source_documents = payload.get("source_documents") or []
    if not isinstance(studies, list):
        studies = []
    if not isinstance(source_documents, list):
        source_documents = []

    category_by_module: Dict[str, str] = {}
    for entry in context.get("mapping") or []:
        if not isinstance(entry, dict):
            continue
        module4 = str(entry.get("module4_section") or "").strip()
        category = str(entry.get("category") or "").strip()
        if module4 and category:
            category_by_module[module4] = category

    source_by_id: Dict[str, Dict[str, Any]] = {}
    source_by_study: Dict[str, Dict[str, Any]] = {}
    for source in source_documents:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("id") or "").strip()
        if source_id:
            source_by_id[source_id] = source
        blob = " ".join(
            str(part)
            for part in (
                source.get("file_name"),
                source.get("ctd_section"),
                source.get("module"),
            )
            if part
        )
        for study_id in _extract_study_ids(blob):
            source_by_study.setdefault(study_id.upper(), source)

    document_keys_by_study: Dict[str, str] = {}
    for key in (context.get("document_keys") or []) + (
        context.get("project_document_keys") or []
    ):
        text = str(key or "").strip()
        if not text:
            continue
        for study_id in _extract_study_ids(text):
            document_keys_by_study.setdefault(study_id.upper(), text)

    records_by_study: Dict[str, Dict[str, Any]] = {}
    for study in studies:
        if not isinstance(study, dict):
            continue
        sponsor_study_id = str(study.get("sponsor_study_id") or "").strip()
        study_number = _canonical_study_number(sponsor_study_id)
        if not study_number:
            continue
        normalized_key = study_number.upper()

        extra = _normalize_extra_attributes(study.get("extra_attributes"))
        source_doc_id = _first_non_empty(
            (
                study.get("main_source_document_id"),
                extra.get("legacy_document_id"),
            )
        )
        source_doc = source_by_id.get(source_doc_id)
        if source_doc is None:
            source_doc = source_by_study.get(normalized_key)

        module4 = _first_non_empty(
            (
                study.get("module4_section"),
                source_doc.get("ctd_section") if isinstance(source_doc, dict) else "",
            )
        )
        if module4_section and not section_number_matches(module4, module4_section):
            continue

        source_title = ""
        if isinstance(source_doc, dict):
            source_title = _display_title_from_path(str(source_doc.get("file_name") or ""))
        if not source_title:
            source_title = _display_title_from_path(
                document_keys_by_study.get(normalized_key, "")
            )
        location_in_ctd = _first_non_empty(
            (
                extra.get("location_in_ctd"),
                (
                    f'Module 4, Section {module4}: "{source_title}"'
                    if module4 and source_title
                    else ""
                ),
                f"Module 4, Section {module4}" if module4 else "",
            )
        )

        species = str(study.get("species") or "").strip()
        strain = str(study.get("strain") or "").strip()
        test_system = _first_non_empty(
            (
                extra.get("test_system"),
                extra.get("species_strain"),
                extra.get("species_or_system"),
                f"{species}; {strain}" if species and strain else "",
                species,
            )
        )

        record = {
            "study_id": str(study.get("id") or "").strip(),
            "study_number": study_number,
            "module4_section": module4,
            "type_of_study": _first_non_empty(
                (
                    extra.get("type_of_study"),
                    study.get("study_type"),
                    extra.get("study_title"),
                    extra.get("title"),
                    category_by_module.get(module4),
                )
            ),
            "species": species,
            "strain": strain,
            "test_system": test_system,
            "method_of_administration": _first_non_empty(
                (
                    extra.get("method_of_administration"),
                    extra.get("route_of_administration"),
                    extra.get("route"),
                    study.get("route"),
                )
            ),
            "testing_facility": _first_non_empty(
                (
                    extra.get("testing_facility"),
                    extra.get("test_facility"),
                    extra.get("facility"),
                    extra.get("laboratory"),
                )
            ),
            "glp_compliance": _first_non_empty(
                (
                    study.get("glp_status"),
                    extra.get("glp_compliance"),
                    extra.get("glp_status"),
                    extra.get("glp"),
                )
            ),
            "location_in_ctd": location_in_ctd,
            "extra": extra,
        }
        score = sum(
            1
            for field in (
                "type_of_study",
                "test_system",
                "method_of_administration",
                "testing_facility",
                "glp_compliance",
                "location_in_ctd",
            )
            if str(record.get(field) or "").strip()
        )
        existing = records_by_study.get(normalized_key)
        if existing is not None and int(existing.get("_score", 0)) >= score:
            continue
        record["_score"] = score
        records_by_study[normalized_key] = record

    records = [
        {k: v for k, v in record.items() if k != "_score"}
        for record in records_by_study.values()
    ]
    records.sort(key=lambda record: _study_sort_key(str(record.get("study_number") or "")))
    return records


def _render_dose_group_summary(groups: Sequence[Dict[str, Any]]) -> Tuple[str, str]:
    doses: List[str] = []
    sex_counts: List[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        dose_value = group.get("dose_mg_per_kg")
        if dose_value is not None and str(dose_value).strip() != "":
            dose_text = str(dose_value).strip()
            if dose_text not in doses:
                doses.append(dose_text)
        sex = str(group.get("sex") or "").strip()
        n_animals = group.get("n_animals")
        if sex and n_animals is not None and str(n_animals).strip() != "":
            sex_counts.append(f"{sex} (n={n_animals})")
        elif sex:
            sex_counts.append(sex)
    return ", ".join(doses), "; ".join(sex_counts)


def _rows_from_token_candidates(
    columns: Sequence[str], candidates: Sequence[Dict[str, str]]
) -> List[Dict[str, str]]:
    passthrough_rows: List[Dict[str, str]] = []
    study_col = next(
        (col for col in columns if _normalize_header_token(col) == "study number"), None
    )
    deduped: Dict[str, Dict[str, Any]] = {}
    for candidate in candidates:
        row = {str(col): "" for col in columns}
        for col in columns:
            token = _normalize_header_token(str(col))
            value = str(candidate.get(token) or "").strip()
            if value:
                row[str(col)] = value
        if not any(str(value).strip() for value in row.values()):
            continue
        if not study_col:
            passthrough_rows.append(row)
            continue
        parsed_ids = _extract_study_ids(str(row.get(study_col) or ""))
        if not parsed_ids:
            passthrough_rows.append(row)
            continue
        canonical_id = parsed_ids[0]
        row[study_col] = canonical_id
        score = sum(1 for value in row.values() if str(value).strip())
        current = deduped.get(canonical_id)
        if current is None or score > int(current.get("_score", 0)):
            row_with_score = dict(row)
            row_with_score["_score"] = score
            deduped[canonical_id] = row_with_score
    if deduped:
        ordered_ids = sorted(deduped.keys(), key=_study_sort_key)
        normalized_rows = [
            {k: v for k, v in deduped[study_id].items() if k != "_score"}
            for study_id in ordered_ids
        ]
        return [*normalized_rows, *passthrough_rows]
    return passthrough_rows


def _extract_primary_pd_candidates(context: Dict[str, Any]) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    ncd_records = _extract_ncd_study_records(context, module4_section="4.2.1.1")
    if ncd_records:
        payload = context.get("ncd_payload") or {}
        dose_groups = payload.get("dose_groups") if isinstance(payload, dict) else []
        dose_groups_by_study: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        if isinstance(dose_groups, list):
            for group in dose_groups:
                if not isinstance(group, dict):
                    continue
                study_id = str(group.get("study_id") or "").strip()
                if study_id:
                    dose_groups_by_study[study_id].append(group)
        for record in ncd_records:
            extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
            study_id = str(record.get("study_id") or "").strip()
            study_dose_groups = dose_groups_by_study.get(study_id, [])
            dose_text, _ = _render_dose_group_summary(study_dose_groups)
            candidates.append(
                {
                    "study number": str(record.get("study_number") or ""),
                    "species strain or test system": _first_non_empty(
                        (
                            record.get("test_system"),
                            f"{record.get('species')}; {record.get('strain')}"
                            if record.get("species") and record.get("strain")
                            else "",
                            record.get("species"),
                        )
                    ),
                    "method of administration": str(
                        record.get("method_of_administration") or ""
                    ),
                    "dose concentration": _first_non_empty(
                        (
                            extra.get("dose_concentration") if extra else "",
                            extra.get("dose_levels") if extra else "",
                            extra.get("dose_level") if extra else "",
                            extra.get("dose") if extra else "",
                            dose_text,
                        )
                    ),
                    "endpoints assays": _first_non_empty(
                        (
                            extra.get("endpoints_assays") if extra else "",
                            extra.get("endpoints") if extra else "",
                            extra.get("assays") if extra else "",
                        )
                    ),
                    "noteworthy findings": _first_non_empty(
                        (
                            extra.get("key_findings") if extra else "",
                            extra.get("noteworthy_findings") if extra else "",
                            extra.get("findings") if extra else "",
                            extra.get("result_summary") if extra else "",
                        )
                    ),
                    "glp compliance": str(record.get("glp_compliance") or ""),
                    "location in ctd": str(record.get("location_in_ctd") or ""),
                }
            )
        return candidates

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
            if not study_number:
                continue
            parsed_ids = _extract_study_ids(study_number)
            canonical_id = parsed_ids[0] if parsed_ids else study_number.strip()
            if not canonical_id or canonical_id.upper() in seen:
                continue
            doses_key = key_map.get("doses")
            candidates.append(
                {
                    "study number": canonical_id,
                    "species strain or test system": _pick_first_value(
                        row,
                        key_map,
                        "species strain",
                        "test system",
                        "species strain or test system",
                    ),
                    "method of administration": _pick_first_value(
                        row, key_map, "method of administration"
                    ),
                    "dose concentration": (
                        str(row.get(doses_key or "") or "").strip() if doses_key else ""
                    ),
                    "endpoints assays": "",
                    "noteworthy findings": _pick_first_value(
                        row,
                        key_map,
                        "noteworthy findings",
                        "key findings",
                        "key results",
                    ),
                    "glp compliance": _pick_first_value(row, key_map, "glp compliance"),
                    "location in ctd": _pick_first_value(row, key_map, "location in ctd"),
                }
            )
            seen.add(canonical_id.upper())
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
    columns = [str(col) for col in (target.get("columns") or []) if str(col).strip()]
    if not columns:
        columns = [
            "Study Number",
            "Species/Strain or Test System",
            "Method of Administration",
            "Dose/Concentration",
            "Endpoints/Assays",
            "Key Findings",
            "GLP Compliance",
            "Location in CTD",
        ]
        target["columns"] = columns
    target["rows"] = _rows_from_token_candidates(columns, candidates)


def _extract_safety_pharmacology_candidates(
    context: Dict[str, Any]
) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    ncd_records = _extract_ncd_study_records(context, module4_section="4.2.1.3")
    if ncd_records:
        payload = context.get("ncd_payload") or {}
        dose_groups = payload.get("dose_groups") if isinstance(payload, dict) else []
        findings = payload.get("findings") if isinstance(payload, dict) else []
        safety_summaries = (
            payload.get("safety_summaries") if isinstance(payload, dict) else []
        )
        findings_by_study: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        summaries_by_study: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        dose_groups_by_study: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        if isinstance(findings, list):
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                study_id = str(finding.get("study_id") or "").strip()
                if study_id:
                    findings_by_study[study_id].append(finding)
        if isinstance(safety_summaries, list):
            for summary in safety_summaries:
                if not isinstance(summary, dict):
                    continue
                study_id = str(summary.get("study_id") or "").strip()
                if study_id:
                    summaries_by_study[study_id].append(summary)
        if isinstance(dose_groups, list):
            for group in dose_groups:
                if not isinstance(group, dict):
                    continue
                study_id = str(group.get("study_id") or "").strip()
                if study_id:
                    dose_groups_by_study[study_id].append(group)
        for record in ncd_records:
            extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
            study_id = str(record.get("study_id") or "").strip()
            study_findings = findings_by_study.get(study_id, [])
            study_summaries = summaries_by_study.get(study_id, [])
            study_dose_groups = dose_groups_by_study.get(study_id, [])
            organ_systems: List[str] = []
            for finding in study_findings:
                organ_system = str(finding.get("organ_system") or "").strip()
                if organ_system and organ_system not in organ_systems:
                    organ_systems.append(organ_system)
            doses_text, sex_group_text = _render_dose_group_summary(study_dose_groups)
            summary_findings = [
                str(summary.get("limiting_finding") or "").strip()
                for summary in study_summaries
                if isinstance(summary, dict)
                and str(summary.get("limiting_finding") or "").strip()
            ]
            finding_terms = [
                str(finding.get("finding_term") or "").strip()
                for finding in study_findings
                if isinstance(finding, dict)
                and str(finding.get("finding_term") or "").strip()
            ][:3]
            findings_text = _first_non_empty(
                (
                    extra.get("noteworthy_findings") if extra else "",
                    extra.get("key_findings") if extra else "",
                    extra.get("findings") if extra else "",
                    "; ".join(summary_findings),
                    "; ".join(finding_terms),
                )
            )
            candidates.append(
                {
                    "organ systems evaluated": _first_non_empty(
                        (
                            extra.get("organ_systems") if extra else "",
                            extra.get("organ_system") if extra else "",
                            "; ".join(organ_systems),
                        )
                    ),
                    "species strain": _first_non_empty(
                        (
                            record.get("test_system"),
                            f"{record.get('species')}; {record.get('strain')}"
                            if record.get("species") and record.get("strain")
                            else "",
                            record.get("species"),
                        )
                    ),
                    "method of administration": str(
                        record.get("method_of_administration") or ""
                    ),
                    "doses": _first_non_empty(
                        (
                            extra.get("dose_levels") if extra else "",
                            extra.get("dose_level") if extra else "",
                            extra.get("dose") if extra else "",
                            doses_text,
                        )
                    ),
                    "gender and no per group": _first_non_empty(
                        (
                            extra.get("gender_group") if extra else "",
                            extra.get("sex_and_n_per_group") if extra else "",
                            sex_group_text,
                        )
                    ),
                    "noteworthy findings": findings_text,
                    "glp compliance": str(record.get("glp_compliance") or ""),
                    "study number": str(record.get("study_number") or ""),
                    "location in ctd": str(record.get("location_in_ctd") or ""),
                }
            )
        return candidates

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
            if not study_number:
                continue
            parsed_ids = _extract_study_ids(study_number)
            canonical_id = parsed_ids[0] if parsed_ids else study_number.strip()
            if not canonical_id or canonical_id.upper() in seen:
                continue
            doses_key = key_map.get("doses")
            candidates.append(
                {
                    "organ systems evaluated": _pick_first_value(
                        row, key_map, "organ systems evaluated"
                    ),
                    "species strain": _pick_first_value(row, key_map, "species strain"),
                    "method of administration": _pick_first_value(
                        row, key_map, "method of administration"
                    ),
                    "doses": (
                        str(row.get(doses_key or "") or "").strip() if doses_key else ""
                    ),
                    "gender and no per group": _pick_first_value(
                        row, key_map, "gender and no per group", "gender", "sex"
                    ),
                    "noteworthy findings": _pick_first_value(
                        row, key_map, "noteworthy findings"
                    ),
                    "glp compliance": _pick_first_value(row, key_map, "glp compliance"),
                    "study number": canonical_id,
                    "location in ctd": _pick_first_value(row, key_map, "location in ctd"),
                }
            )
            seen.add(canonical_id.upper())
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
    columns = [str(col) for col in (target.get("columns") or []) if str(col).strip()]
    if not columns:
        columns = [
            "Organ Systems Evaluated",
            "Species/Strain",
            "Method of Admin.",
            "Doses (mg/kg)",
            "Gender and No. per Group",
            "Noteworthy Findings",
            "GLP Compliance",
            "Study Number",
            "Location in CTD",
        ]
        target["columns"] = columns
    target["rows"] = _rows_from_token_candidates(columns, candidates)


def _first_non_empty(values: Sequence[Any]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _display_title_from_path(value: str) -> str:
    if not value:
        return ""
    name = Path(str(value)).name
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name.strip()


def _extract_overview_candidates_from_ncd(
    context: Dict[str, Any]
) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    for record in _extract_ncd_study_records(context):
        candidates.append(
            {
                "type_of_study": str(record.get("type_of_study") or ""),
                "test_system": str(record.get("test_system") or ""),
                "method_of_administration": str(
                    record.get("method_of_administration") or ""
                ),
                "testing_facility": str(record.get("testing_facility") or ""),
                "study_number": str(record.get("study_number") or ""),
                "location_in_ctd": str(record.get("location_in_ctd") or ""),
            }
        )
    return candidates


def _extract_overview_candidates(context: Dict[str, Any]) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    seen: set[str] = set()
    ncd_ids: set[str] = set()
    for candidate in _extract_overview_candidates_from_ncd(context):
        study_number = str(candidate.get("study_number") or "").strip()
        if not study_number:
            continue
        key = study_number.upper()
        if key in seen:
            continue
        candidates.append(candidate)
        seen.add(key)
        ncd_ids.add(key)

    logger.debug(
        "overview: table_assets=%s preview_rows_total=%s section_sources=%s",
        len(context.get("table_assets") or []),
        sum(len(a.get("preview_rows") or []) for a in context.get("table_assets") or []),
        len(context.get("section_sources") or []),
    )
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
            if ncd_ids and study_id.upper() not in ncd_ids:
                continue
            if study_id.upper() in seen:
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
                    "location_in_ctd": str(
                        row.get(key_map.get("location in ctd") or "") or ""
                    ).strip(),
                    "study_number": study_id,
                }
            )
            seen.add(study_id.upper())
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
    location_col = next(
        (
            col
            for col in columns
            if _normalize_header_token(col) == "location in ctd"
        ),
        None,
    )

    candidates = _extract_overview_candidates(context)
    if not candidates:
        return
    allowed_ids: set[str] = set()
    for candidate in candidates:
        for study_id in _extract_study_ids(str(candidate.get("study_number") or "")):
            allowed_ids.add(study_id.upper())

    def candidate_text(candidate: Dict[str, str]) -> str:
        parts = [
            candidate.get("type_of_study", ""),
            candidate.get("test_system", ""),
            candidate.get("method_of_administration", ""),
            candidate.get("testing_facility", ""),
            candidate.get("location_in_ctd", ""),
        ]
        return " ".join(part for part in parts if part)

    candidate_tokens = [(_tokenize_text(candidate_text(c)), c) for c in candidates]
    candidate_index_by_study = {
        candidate.get("study_number", ""): idx
        for idx, candidate in enumerate(candidates)
    }

    matched_indices: set[int] = set()
    rows = target.get("rows")
    if not isinstance(rows, list):
        rows = []
        target["rows"] = rows
    for row in rows:
        if not isinstance(row, dict):
            continue
        current = str(row.get(study_col) or "").strip()
        normalized_current = _extract_study_ids(current)
        if current and not normalized_current:
            row[study_col] = ""
            current = ""
        if normalized_current:
            canonical_id = normalized_current[0]
            if allowed_ids and canonical_id.upper() not in allowed_ids:
                row[study_col] = ""
                continue
            row[study_col] = canonical_id
            if canonical_id in candidate_index_by_study:
                matched_indices.add(candidate_index_by_study[canonical_id])
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
            elif key == "location in ctd":
                new_row[col] = candidate.get("location_in_ctd", "")
            elif key == "study number":
                new_row[col] = candidate.get("study_number", "")
        rows.append(new_row)

    candidate_by_id = {
        str(candidate.get("study_number") or "").strip().upper(): candidate
        for candidate in candidates
        if candidate.get("study_number")
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        current_ids = _extract_study_ids(str(row.get(study_col) or ""))
        if not current_ids:
            continue
        canonical_id = current_ids[0]
        if allowed_ids and canonical_id.upper() not in allowed_ids:
            continue
        row[study_col] = canonical_id
        candidate = candidate_by_id.get(canonical_id.upper())
        if not candidate:
            continue
        for col in columns:
            if str(row.get(col) or "").strip():
                continue
            key = _normalize_header_token(col)
            if key == "type of study":
                row[col] = candidate.get("type_of_study", "")
            elif key == "test system":
                row[col] = candidate.get("test_system", "")
            elif key == "method of administration":
                row[col] = candidate.get("method_of_administration", "")
            elif key == "testing facility":
                row[col] = candidate.get("testing_facility", "")
            elif key == "location in ctd":
                row[col] = candidate.get("location_in_ctd", "")
        if (
            location_col
            and not str(row.get(location_col) or "").strip()
            and candidate.get("location_in_ctd")
        ):
            row[location_col] = candidate.get("location_in_ctd", "")

    deduped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        study_id = str(row.get(study_col) or "").strip()
        parsed_ids = _extract_study_ids(study_id)
        if not parsed_ids:
            continue
        canonical_id = parsed_ids[0]
        if allowed_ids and canonical_id.upper() not in allowed_ids:
            continue
        row[study_col] = canonical_id
        score = sum(1 for col in columns if str(row.get(col) or "").strip())
        current = deduped.get(canonical_id)
        if not current or score > current.get("_score", 0):
            row_copy = dict(row)
            row_copy["_score"] = score
            deduped[canonical_id] = row_copy
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
    ncd_payload: Dict[str, Any] = {}
    try:
        ncd_payload = fetch_ncd_payload(
            db,
            project_id=project_id,
            module4_sections=module4_sections,
        )
    except Exception as exc:
        ncd_payload = {}
        logger.warning(
            "tabulated ctx: failed to fetch ncd payload for section=%s project=%s: %s",
            section,
            project_id,
            exc,
        )

    available_sections = _sections_with_material_data(
        module4_sections,
        section_sources=sources,
        document_keys=document_keys,
        ncd_payload=ncd_payload,
    )
    if set(available_sections) != set(module4_sections):
        module4_sections = available_sections
        mapping_entries = _filter_mapping_entries_by_module4_sections(
            mapping_entries,
            module4_sections,
        )
        sources = fetch_section_sources(
            db,
            tenant_id=tenant_id,
            bucket=bucket,
            project_like=project_like,
            module4_sections=module4_sections,
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
        try:
            ncd_payload = fetch_ncd_payload(
                db,
                project_id=project_id,
                module4_sections=module4_sections,
            )
        except Exception as exc:
            ncd_payload = {}
            logger.warning(
                "tabulated ctx: failed to refetch ncd payload for section=%s project=%s: %s",
                section,
                project_id,
                exc,
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
    selected_assets: List[Dict[str, Any]] = []
    if max_tables > 0 and assets:
        assets_by_document: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in assets:
            key = str(row.get("s3_key") or "")
            document_key = key.split(".pdf.tables/", 1)[0] if key else ""
            assets_by_document[document_key].append(row)
        document_asset_keys = sorted(assets_by_document.keys())
        while len(selected_assets) < max_tables:
            added_in_round = False
            for document_key in document_asset_keys:
                bucket_rows = assets_by_document.get(document_key) or []
                if not bucket_rows:
                    continue
                selected_assets.append(bucket_rows.pop(0))
                added_in_round = True
                if len(selected_assets) >= max_tables:
                    break
            if not added_in_round:
                break

    for row in selected_assets:
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
        else:
            logger.debug(
                "tabulated ctx: asset %s missing json_key (section=%s, module4=%s, page=%s idx=%s)",
                row.get("id"),
                section,
                row.get("section_number") if "section_number" in row else None,
                row.get("page_number"),
                row.get("index_on_page"),
            )
        if not preview_rows:
            logger.debug(
                "tabulated ctx: no preview_rows for asset %s (json_key=%s, columns=%s, row_count=%s)",
                row.get("id"),
                json_key,
                extra.get("columns"),
                extra.get("row_count"),
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

    mapped_target_sections = _mapped_target_sections_for_request(
        section=section,
        mapping_entries=mapping_entries,
    )
    if section.startswith("2.6."):
        table_specs = _filter_section_entries_for_targets(
            table_specs,
            target_sections=mapped_target_sections,
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
    ncd_studies_count = len(ncd_payload.get("studies") or [])
    ncd_source_documents_count = len(ncd_payload.get("source_documents") or [])

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
        "ncd_studies_count": ncd_studies_count,
        "ncd_source_documents_count": ncd_source_documents_count,
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
        "ncd_payload": ncd_payload,
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


def _build_topic_title(text: str, max_words: int = 12) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return "Topic"
    words = cleaned.split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]) + " ..."


def _attach_assets_to_topic(
    *,
    assets: Sequence[Dict[str, Any]],
    document_version_id: str,
    page_start: Optional[int],
    page_end: Optional[int],
) -> Dict[str, List[Dict[str, Any]]]:
    images: List[Dict[str, Any]] = []
    tables: List[Dict[str, Any]] = []
    window_start = page_start
    window_end = page_end
    for asset in assets:
        if str(asset.get("document_version_id")) != str(document_version_id):
            continue
        page_number = asset.get("page_number")
        if window_start is not None and page_number is not None:
            if window_end is None:
                window_end = window_start
            # keep assets on the same page or adjacent pages
            if page_number < window_start - 1 or page_number > window_end + 1:
                continue
        target = tables if asset.get("asset_type") == "table" else images
        target.append(asset)
    return {"images": images, "tables": tables}


def _render_table_html(
    columns: Sequence[str],
    rows: Sequence[Dict[str, Any]],
    caption: Optional[str] = None,
    max_rows: int = 20,
) -> str:
    cols = [str(c) for c in columns] if columns else []
    body_rows = rows[:max_rows] if rows else []
    if not cols and body_rows:
        # derive order from first row
        cols = list(body_rows[0].keys())
    thead = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    tbody_parts: List[str] = []
    for row in body_rows:
        cells = "".join(
            f"<td>{html.escape(str(row.get(col, '')))}</td>" for col in cols
        )
        tbody_parts.append(f"<tr>{cells}</tr>")
    caption_html = f"<caption>{html.escape(caption)}</caption>" if caption else ""
    return f"<table>{caption_html}<thead><tr>{thead}</tr></thead><tbody>{''.join(tbody_parts)}</tbody></table>"


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
        raise HTTPException(status_code=500, detail=f"Invalid S3 folder template: {exc}")

    folder_keys = [base_prefix, *_collect_s3_folder_keys(base_prefix, structure)]

    def worker() -> Dict[str, Any]:
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
        raise HTTPException(
            status_code=502, detail=f"Failed to create project folders: {exc}"
        ) from exc


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
