"""Stage that parses markdown into section-based chunks."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import boto3

from ind_pipeline.utils import parse_s3_uri
from ncd.ingestion.section_detector import extract_section_spans, persist_section_spans
from ncd.db import SessionLocal

from ind_pipeline.stage_common import SimpleStageModule, StageModuleSpec

logger = logging.getLogger(__name__)
_s3_client = boto3.client("s3")


def _workload(payload: Dict[str, object]) -> Dict[str, object]:
    """Parse markdown into section spans and emit chunk metadata."""
    markdown = _resolve_markdown(payload)
    if not markdown:
        return {
            "notes": "No markdown found for section parsing.",
            "chunk_count": 0,
            "chunks": [],
        }

    sections = extract_section_spans(markdown)
    include_text = os.getenv("SECTION_CHUNK_INCLUDE_TEXT", "0") == "1"
    chunks = _build_chunks(sections, markdown, include_text=include_text)

    persisted = None
    if os.getenv("SECTION_INDEXING_WRITE_DB", "0") == "1":
        document_version_id = _resolve_document_version_id(payload)
        if document_version_id:
            persisted = _persist_sections(document_version_id, sections)
        else:
            persisted = "skipped: missing document_version_id"

    return {
        "notes": "Parsed markdown into section chunks.",
        "chunk_count": len(chunks),
        "chunks": chunks,
        "section_numbers": [chunk["section_number"] for chunk in chunks],
        "persisted": persisted,
    }


def _resolve_markdown(payload: Dict[str, object]) -> Optional[str]:
    for candidate in _iter_payload_candidates(payload):
        direct = candidate.get("markdown")
        if isinstance(direct, str) and direct.strip():
            return direct

        analysis = candidate.get("analysis")
        if isinstance(analysis, dict):
            analysis_markdown = analysis.get("markdown")
            if isinstance(analysis_markdown, str) and analysis_markdown.strip():
                return analysis_markdown

        analysis_uri = candidate.get("analysis_s3_uri") or candidate.get("analysis_uri")
        if isinstance(analysis_uri, str) and analysis_uri.strip():
            markdown = _load_markdown_from_analysis(str(analysis_uri))
            if markdown:
                return markdown

        markdown_uri = candidate.get("markdown_s3_uri")
        if isinstance(markdown_uri, str) and markdown_uri.strip():
            markdown = _load_markdown_from_s3(str(markdown_uri))
            if markdown:
                return markdown

    return None


def _load_markdown_from_analysis(s3_uri: str) -> Optional[str]:
    try:
        bucket, key = parse_s3_uri(s3_uri)
        obj = _s3_client.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read()
        analysis = json.loads(body.decode("utf-8"))
        markdown = analysis.get("markdown")
        if isinstance(markdown, str):
            return markdown
    except Exception as exc:  # pragma: no cover - network failure
        logger.warning("Failed to load analysis markdown from %s: %s", s3_uri, exc)
    return None


def _load_markdown_from_s3(s3_uri: str) -> Optional[str]:
    try:
        bucket, key = parse_s3_uri(s3_uri)
        obj = _s3_client.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read().decode("utf-8")
    except Exception as exc:  # pragma: no cover - network failure
        logger.warning("Failed to load markdown from %s: %s", s3_uri, exc)
    return None


def _build_chunks(
    sections: List[Any],
    markdown: str,
    *,
    include_text: bool,
) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    for idx, section in enumerate(sections, start=1):
        chunk: Dict[str, Any] = {
            "chunk_index": idx,
            "section_number": section.section_number,
            "section_title": section.section_title,
            "char_start": section.char_start,
            "char_end": section.char_end,
            "page_start": section.page_start,
            "page_end": section.page_end,
        }
        if include_text:
            chunk["text"] = markdown[section.char_start : section.char_end]
        chunks.append(chunk)
    return chunks


def _resolve_document_version_id(payload: Dict[str, object]) -> Optional[str]:
    for candidate in _iter_payload_candidates(payload):
        direct = candidate.get("document_version_id")
        if isinstance(direct, str) and direct.strip():
            return direct
        metadata = candidate.get("metadata")
        if isinstance(metadata, dict):
            value = metadata.get("document_version_id")
            if isinstance(value, str) and value.strip():
                return value
    return None


def _iter_payload_candidates(payload: Dict[str, object]) -> List[Dict[str, object]]:
    candidates: List[Dict[str, object]] = []
    if isinstance(payload, dict):
        candidates.append(payload)
        output = payload.get("output")
        if isinstance(output, dict):
            candidates.append(output)
        inner = payload.get("input")
        if isinstance(inner, dict):
            candidates.append(inner)
    return candidates


def _persist_sections(document_version_id: str, sections: List[Any]) -> str:
    if not sections:
        return "skipped: no sections"
    db = SessionLocal()
    try:
        count = persist_section_spans(db, document_version_id, sections)
        return f"persisted:{count}"
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Section persistence failed: %s", exc)
        db.rollback()
        return f"failed:{exc}"
    finally:
        db.close()


_MODULE = SimpleStageModule(
    StageModuleSpec(
        module_name="pdf-parsing-chunking",
        env_prefix="PDF_PARSING_CHUNKING",
        description="Parse documents, extract text/tables, and chunk by logical sections.",
        default_queue_name="pdf-parsing-chunking-queue",
    ),
    work_fn=_workload,
)


def handle_message(payload: Dict[str, object]) -> Dict[str, object]:
    """Expose the handler for NotificationConsumer wiring."""
    return _MODULE.handler(payload)


def run() -> None:
    """Start the module's consumer loop."""
    _MODULE.run()


__all__ = ["handle_message", "run"]
