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
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, cast, NoReturn
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
from pydantic import ValidationError
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
    DEV_ROUTER_PREFIX,
    DEV_ROUTER_TAGS,
    NCD_ROUTER_PREFIX,
    NCD_ROUTER_TAGS,
    SECTION_PROMPT_MAX_CHARS,
    USER_PROMPT_MAX_CHARS as _USER_PROMPT_MAX_CHARS,
    STUDY_ID_EXT_RE,
    STUDY_ID_PREFIX_RE,
    STUDY_ID_RE,
    STUDY_ID_SKIP_RE,
    STUDY_ID_TRAILERS,
    UPLOAD_ROUTER_TAGS,
)
from pdf_analysis.api.request_utils import (
    _normalize_json_dict,
    _normalize_optional_uuid,
    _normalize_user_prompt,
    _normalize_uuid_list,
    _require_uuid,
)
from pdf_analysis.api import rate_limit as _rate_limit_utils
from pdf_analysis.api.gap_analysis import (
    _build_gap_analysis_report,
    _gap_collect_required_fields,
    _gap_field_is_present,
    _gap_key_mentions_module4_section,
    _gap_parse_module_number,
)
from pdf_analysis.api.schemas import (
    CTDSectionSummaryApproveRequest,
    CTDSectionSummaryRequest,
    CTDTabulatedSummaryApproveRequest,
    CTDTabulatedSummaryRequest,
    NCDGapAnalysisRequest,
    NCDLabelRequest,
    NCDRelabelRequest,
    S3AnalyzeRequest,
    S3MarkdownRequest,
    S3MarkdownUploadRequest,
    S3NewProjectRequest,
    TemplateOverrideRequest,
)
from pdf_analysis.api.s3_utils import (
    _analysis_json_key,
    _boto3_client,
    _collect_s3_folder_keys,
    _list_docx_objects,
    _list_template_objects,
    _load_s3_folder_template,
    _metadata_json_key,
    _normalize_extra_attributes,
    _normalize_prefix_list,
    _normalize_s3_path_segment,
    _normalize_s3_prefix,
    _read_s3_text,
    _s3_prefix_exists,
    _update_object_metadata,
    _upload_analysis_json_to_s3,
    _upload_metadata_json_to_s3,
)
from pdf_analysis.api.summary_helpers import *  # noqa: F401,F403
from pdf_analysis.api.table_utils import (
    _build_table_manifest,
    _parse_columns_header,
    _table_to_payload,
    _truncate_preview_value,
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
    """Health check."""
    return {"status": "ok"}



logger = logging.getLogger(__name__)

_RATE_LIMIT_LOCK = _rate_limit_utils._RATE_LIMIT_LOCK
_RATE_LIMIT_BUCKETS = _rate_limit_utils._RATE_LIMIT_BUCKETS
_LLM_RATE_LIMIT_POLICIES = _rate_limit_utils._LLM_RATE_LIMIT_POLICIES
_resolve_request_client_id = _rate_limit_utils._resolve_request_client_id
_consume_rate_limit_token = _rate_limit_utils._consume_rate_limit_token
_enforce_llm_endpoint_rate_limit = _rate_limit_utils._enforce_llm_endpoint_rate_limit
USER_PROMPT_MAX_CHARS = _USER_PROMPT_MAX_CHARS


def _raise_sanitized_http_error(
    *,
    status_code: int,
    detail: str,
    exc: Exception,
    log_message: str,
) -> NoReturn:
    """Log full exception details server-side while returning a safe client message."""
    logger.exception(log_message)
    raise HTTPException(status_code=status_code, detail=detail) from exc


@app.middleware("http")
async def log_unhandled_exceptions(request: Request, call_next):
    """Log unhandled exceptions."""
    try:
        return await call_next(request)
    except HTTPException:
        # Let FastAPI handle expected HTTP errors
        raise
    except Exception:
        logger.exception("Unhandled error for %s %s", request.method, request.url.path)
        raise


_ANALYZE_REQUEST_BODY_OPENAPI = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/S3AnalyzeRequest"}
        },
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "required": ["file"],
                "properties": {
                    "file": {
                        "type": "string",
                        "format": "binary",
                        "description": "PDF file to analyze.",
                    }
                },
            }
        },
    },
}


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
    """Get embedding client."""
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
    """Embedding encoding."""
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
    """Approx token count."""
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
    """Split text for embedding."""
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
    """Weighted average embeddings."""
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
    """Request embedding."""
    response = client.embeddings.create(
        model=settings.embedding_model_name,
        input=text,
    )
    if not response.data:
        return None
    return list(response.data[0].embedding)


def _generate_embedding(text: str, expected_dim: int = 1536) -> Optional[List[float]]:
    """Generate embedding."""
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


def _require_valid_user_id(db: Session, user_id: str) -> str:
    """Require valid user id."""
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


# ---------------------------------------------------------------------------
# Section labeling helpers (IND 2.4/2.6 + CTD section list)
# ---------------------------------------------------------------------------
_IND_TEMPLATE_SECTIONS: List[Dict[str, str]] | None = None
_IND_TEMPLATE_ENTRIES: List[Dict[str, Any]] | None = None
_SECTION_LIST: List[Dict[str, Any]] | None = None
_CTD_SECTION_SECTIONS: List[Dict[str, str]] | None = None


def _section_list_path() -> Path:
    """Section list path."""
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
    """Reset ind template cache."""
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
    """Section module."""
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
    """Select label classification."""
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
        """Worker."""
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
        _raise_sanitized_http_error(
            status_code=502,
            detail="Failed to download S3 object.",
            exc=exc,
            log_message="Failed to download S3 object",
        )


from pdf_analysis.api.upload_routes import (
    _run_pipeline_with_runner,
    _sync_server_globals as _sync_upload_route_globals,
    upload_router,
)
from pdf_analysis.api.ncd_routes import (
    _sync_server_globals as _sync_ncd_route_globals,
    dev_router,
    ncd_router,
)

_sync_upload_route_globals()
_sync_ncd_route_globals()

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
