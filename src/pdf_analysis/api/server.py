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

import pandas as pd
from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from rapidfuzz import fuzz

from pdf_analysis.ingest.pdf_text import extract_pages_text
from pdf_analysis.ingest.tables import extract_tables_all
from pdf_analysis.pipeline import PDFProcessingPipeline
from pdf_analysis.service.ai_metadata import OpenAIMetadataGenerator, _extract_text_from_response
from pdf_analysis.service.document_summarizer import (
    OpenAIDocumentSummarizer,
    embed_topics_into_markdown,
    format_summary_text,
)

try:
    from pdf_analysis.service.embedding_store import SupabaseEmbeddingStore
except Exception:  # pragma: no cover - optional dependency or missing extras
    SupabaseEmbeddingStore = None  # type: ignore[misc,assignment]
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
upload_router = APIRouter(tags=["upload"])
ncd_router = APIRouter(prefix="/ncd", tags=["ncd"])
dev_router = APIRouter(prefix="/dev", tags=["dev"])

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
        logger.warning("Failed to upload analysis payload for %s: %s", analysis_key, exc)
    return analysis_key


# ---------------------------------------------------------------------------
# Section labeling helpers (Module 2.4 / 2.6 template-driven)
# ---------------------------------------------------------------------------
_IND_TEMPLATE_SECTIONS: List[Dict[str, str]] | None = None


def _load_ind_template_sections() -> List[Dict[str, str]]:
    """Load section numbers/titles from the IND 2.4/2.6 template."""
    global _IND_TEMPLATE_SECTIONS
    if _IND_TEMPLATE_SECTIONS is not None:
        return _IND_TEMPLATE_SECTIONS

    template_path = Path(__file__).resolve().parents[2] / "ncd" / "ind_24_26_template.json"
    try:
        raw = json.loads(template_path.read_text())
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Unable to load template %s: %s", template_path, exc)
        _IND_TEMPLATE_SECTIONS = []
        return _IND_TEMPLATE_SECTIONS

    entries: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        entries = [entry for entry in raw if isinstance(entry, dict)]
    elif isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, list):
                entries.extend([entry for entry in value if isinstance(entry, dict)])

    sections: List[Dict[str, str]] = []
    for entry in entries:
        section_id = str(entry.get("Section") or "").strip()
        if not section_id:
            continue
        title = str(
            entry.get("Subsection Header")
            or entry.get("Section Header")
            or section_id
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


def _match_section_regex(text: str, sections: List[Dict[str, str]]) -> Tuple[str, str, float] | None:
    """Find direct section-number mentions in text."""
    for entry in sections:
        sec = entry["section"]
        if not sec:
            continue
        pattern = rf"\b{re.escape(sec)}\b"
        if re.search(pattern, text):
            return sec, entry["title"], 0.98
    return None


def _score_sections_similarity(text: str, sections: List[Dict[str, str]]) -> Tuple[str, str, float] | None:
    """Score sections using fuzzy similarity against headers/content."""
    if not text.strip():
        return None
    sample = text.lower()[:8000]
    best: Tuple[str, str, float] | None = None
    for entry in sections:
        title = entry["title"].lower()
        blob = entry["blob"].lower() if entry["blob"] else title
        score = max(fuzz.partial_ratio(sample, title), fuzz.partial_ratio(sample, blob)) / 100.0
        if best is None or score > best[2]:
            best = (entry["section"], entry["title"], round(score, 3))
    return best


def _guess_section_from_name(name: str, sections: List[Dict[str, str]]) -> Tuple[str, str, float] | None:
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
        score = max(fuzz.partial_ratio(sample, title), fuzz.partial_ratio(sample, blob)) / 100.0
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


def _llm_select_section(text: str, sections: List[Dict[str, str]]) -> Tuple[str, str, float] | None:
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
    user_prompt = (
        f"Options:\n{options}\n\n"
        "PDF excerpt (trimmed):\n"
        f"{text[:4000]}"
    )
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

    page_limit = payload.page_limit if payload.page_limit and payload.page_limit > 0 else 5

    def worker() -> Dict[str, Any]:
        s3_client = boto3.client("s3", region_name=payload.aws_region)
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
            raise HTTPException(status_code=status, detail=f"Failed to fetch S3 object: {exc}") from exc

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
            raise HTTPException(status_code=400, detail="company/project must be provided for labeling.")

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
            raise HTTPException(status_code=502, detail=f"Failed to copy PDF to labeled folder: {exc}") from exc

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
        candidates = _top_section_candidates(sample_text, _load_ind_template_sections(), limit=5)
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
            {"section_number": section_number, "section_title": section_title or section_number, "score": confidence}
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
        raise HTTPException(status_code=502, detail=f"Failed to download S3 object: {exc}") from exc


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


@upload_router.post("/s3/upload-analyze")
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
            pages = extract_pages_text(pdf_path, ocr_fallback=True, max_pages=page_limit)
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
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

        if not result.markdown:
            raise RuntimeError("Markdown extraction failed for the specified object")

        summary_result = _summary_generator(result.markdown)
        if not summary_result:
            raise RuntimeError("Summary generation failed for the specified object")

        summary_text = format_summary_text(summary_result)
        markdown_with_topics = embed_topics_into_markdown(result.markdown, summary_result.topics)
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
        raise HTTPException(status_code=502, detail=f"Failed to download S3 object: {exc}") from exc
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
