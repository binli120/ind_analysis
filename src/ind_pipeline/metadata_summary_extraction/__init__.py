# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Stage for section summary + keyword extraction."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Sequence

try:
    import boto3
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    boto3 = None  # type: ignore[assignment]

from ind_pipeline.utils import parse_s3_uri
from ncd.database.db import SessionLocal
from ncd.ingestion.section_detector import SectionSpan, extract_section_spans, persist_section_spans
from ncd.llm.llm_client import LLMClient
from sqlalchemy import text as sqltext

from ind_pipeline.stage_common import SimpleStageModule, StageModuleSpec

logger = logging.getLogger(__name__)
_s3_client: Any | None = None
_SUMMARY_PURPOSE = os.getenv("SECTION_SUMMARY_PURPOSE", "ctd_2_6")
_SUMMARY_TYPE = os.getenv("SECTION_SUMMARY_TYPE", "abstractive")
_MIN_KEYWORDS = int(os.getenv("SECTION_SUMMARY_KEYWORDS_MIN", "5"))
_MAX_KEYWORDS = int(os.getenv("SECTION_SUMMARY_KEYWORDS_MAX", "10"))
_MAX_CHARS = int(os.getenv("SECTION_SUMMARY_MAX_CHARS", "12000"))
_MIN_CHARS = int(os.getenv("SECTION_SUMMARY_MIN_CHARS", "400"))


def _workload(payload: Dict[str, object]) -> Dict[str, object]:
    """Generate per-section summaries and keywords using LLMClient."""
    markdown = _resolve_markdown(payload)
    if not markdown:
        return {
            "notes": "No markdown found for section summary extraction.",
            "summary_count": 0,
        }

    sections = _resolve_sections(payload, markdown)
    if not sections:
        return {
            "notes": "No section spans detected for summary extraction.",
            "summary_count": 0,
        }

    llm = LLMClient()
    summary_rows = _summarize_sections(llm, markdown, sections)

    persisted = None
    if os.getenv("SECTION_SUMMARY_WRITE_DB", "0") == "1":
        document_version_id = _resolve_document_version_id(payload)
        if document_version_id:
            persisted = _persist_summaries(
                document_version_id,
                sections,
                summary_rows,
                model_name=llm.model_name,
            )
        else:
            persisted = "skipped: missing document_version_id"

    return {
        "notes": "Generated section summaries.",
        "summary_count": len(summary_rows),
        "summary_purpose": _SUMMARY_PURPOSE,
        "summary_type": _SUMMARY_TYPE,
        "model": llm.model_name,
        "persisted": persisted,
        "section_numbers": [row["section_number"] for row in summary_rows],
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
        obj = _get_s3_client().get_object(Bucket=bucket, Key=key)
        analysis = json.loads(obj["Body"].read().decode("utf-8"))
        markdown = analysis.get("markdown")
        if isinstance(markdown, str):
            return markdown
    except Exception as exc:  # pragma: no cover - network failure
        logger.warning("Failed to load analysis markdown from %s: %s", s3_uri, exc)
    return None


def _load_markdown_from_s3(s3_uri: str) -> Optional[str]:
    try:
        bucket, key = parse_s3_uri(s3_uri)
        obj = _get_s3_client().get_object(Bucket=bucket, Key=key)
        return obj["Body"].read().decode("utf-8")
    except Exception as exc:  # pragma: no cover - network failure
        logger.warning("Failed to load markdown from %s: %s", s3_uri, exc)
    return None


def _resolve_sections(markdown_payload: Dict[str, object], markdown: str) -> List[SectionSpan]:
    for candidate in _iter_payload_candidates(markdown_payload):
        payload_sections = candidate.get("chunks")
        if not isinstance(payload_sections, list):
            continue
        spans: List[SectionSpan] = []
        has_offsets = False
        for item in payload_sections:
            if not isinstance(item, dict):
                continue
            section_number = str(item.get("section_number") or "").strip()
            if not section_number:
                continue
            char_start = _coerce_int(item.get("char_start"), 0)
            char_end = _coerce_int(item.get("char_end"), 0)
            if char_end > char_start:
                has_offsets = True
            spans.append(
                SectionSpan(
                    section_number=section_number,
                    section_title=_optional_str(item.get("section_title")),
                    char_start=char_start,
                    char_end=char_end,
                    page_start=_optional_int(item.get("page_start")),
                    page_end=_optional_int(item.get("page_end")),
                )
            )
        if spans and has_offsets:
            return spans
    return extract_section_spans(markdown)


def _summarize_sections(
    llm: LLMClient,
    markdown: str,
    sections: Sequence[SectionSpan],
) -> List[Dict[str, Any]]:
    summaries: List[Dict[str, Any]] = []
    for section in sections:
        if section.char_end <= section.char_start:
            continue
        text = markdown[section.char_start : section.char_end].strip()
        if _is_trivial_section(text):
            continue
        snippet = text[:_MAX_CHARS]
        if len(snippet) < _MIN_CHARS:
            continue
        payload = _summarize_section(llm, section, snippet)
        if not payload:
            continue
        summaries.append(payload)
    return summaries


def _summarize_section(
    llm: LLMClient,
    section: SectionSpan,
    text: str,
) -> Optional[Dict[str, Any]]:
    topic_hint = "3-6" if len(text) > 4000 else "0-3"
    system_prompt = (
        "You are a regulatory nonclinical analyst. Summarize Module 4 content "
        "into CTD-ready summaries. Return JSON with keys: "
        '"summary" (concise, 3-6 sentences), '
        '"keywords" (array of 5-10 short, domain-specific keywords, each 1-3 words), '
        f'and "topics" (array of objects with keys "topic" and "summary"; include {topic_hint} topics '
        "based on length). Do not use markdown. Do not invent data. If information is missing, omit it."
    )
    title = section.section_title or "N/A"
    user_prompt = (
        f"Section number: {section.section_number}\n"
        f"Section title: {title}\n"
        f"Summary purpose: {_SUMMARY_PURPOSE}\n\n"
        "Content:\n"
        f"{text}"
    )
    try:
        data = llm.extract_json(system_prompt, user_prompt)
    except Exception as exc:
        logger.warning("Summary generation failed for %s: %s", section.section_number, exc)
        data = {}

    summary = str(data.get("summary") or "").strip()
    keywords = _clean_keywords(data.get("keywords"))
    if len(keywords) < _MIN_KEYWORDS:
        keywords = _pad_keywords(keywords, section)

    topics = _normalize_topics(data.get("topics"))
    if not summary:
        summary = "Summary unavailable."

    summary_payload: Dict[str, Any] = {"summary": summary}
    if topics:
        summary_payload["topics"] = topics

    return {
        "section_number": section.section_number,
        "section_title": section.section_title,
        "summary_text": summary_payload,
        "keywords": keywords,
    }


def _normalize_topics(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    topics: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        topic = str(item.get("topic") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not topic or not summary:
            continue
        topics.append({"topic": topic, "summary": summary})
    return topics


def _normalize_summary_json(summary_text: Any, keywords: Sequence[str]) -> str:
    payload: Any
    if isinstance(summary_text, (dict, list)):
        payload = summary_text
    elif isinstance(summary_text, str):
        stripped = summary_text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = {"summary": summary_text}
        else:
            payload = {"summary": summary_text}
    else:
        payload = {"summary": str(summary_text)}

    if isinstance(payload, dict) and "keywords" not in payload and keywords:
        payload["keywords"] = list(keywords)
    return json.dumps(payload, ensure_ascii=True)


def _clean_keywords(raw: Any) -> List[str]:
    if isinstance(raw, str):
        candidates = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, list):
        candidates = [str(item).strip() for item in raw if str(item).strip()]
    else:
        candidates = []

    cleaned: List[str] = []
    seen: set[str] = set()
    for item in candidates:
        if not item:
            continue
        normalized = item.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(item)
        if len(cleaned) >= _MAX_KEYWORDS:
            break

    if len(cleaned) < _MIN_KEYWORDS:
        return cleaned
    return cleaned


def _is_trivial_section(text: str) -> bool:
    if not text:
        return True
    lowered = text.lower()
    if "no extractable text" in lowered:
        return True
    return len(text.strip()) < _MIN_CHARS


def _pad_keywords(existing: Sequence[str], section: SectionSpan) -> List[str]:
    candidates: List[str] = []
    if section.section_title:
        candidates.extend(_split_keywords(section.section_title))
    candidates.append(f"section {section.section_number}")
    candidates.extend(
        [
            "nonclinical",
            "module 4",
            "ctd 2.6",
            "insufficient data",
        ]
    )

    merged: List[str] = []
    seen: set[str] = set()
    for item in list(existing) + candidates:
        if not item:
            continue
        normalized = item.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(item.strip())
        if len(merged) >= _MAX_KEYWORDS:
            break

    if len(merged) < _MIN_KEYWORDS and merged:
        filler = "nonclinical"
        while len(merged) < _MIN_KEYWORDS:
            merged.append(filler)
    return merged


def _split_keywords(text: str) -> List[str]:
    tokens = [part.strip() for part in text.replace("/", " ").split()]
    cleaned: List[str] = []
    for token in tokens:
        token = token.strip(" ,.;:()[]{}")
        if len(token) < 3:
            continue
        if not any(ch.isalpha() for ch in token):
            continue
        cleaned.append(token.lower())
    return cleaned[: _MAX_KEYWORDS]


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


def _persist_summaries(
    document_version_id: str,
    sections: Sequence[SectionSpan],
    summaries: Sequence[Dict[str, Any]],
    *,
    model_name: str,
) -> str:
    if not summaries:
        return "skipped: no summaries"
    db = SessionLocal()
    try:
        persist_section_spans(db, document_version_id, sections, replace_existing=False)
        section_map = _fetch_section_ids(db, document_version_id, sections)
        stored = 0
        for summary in summaries:
            section_number = summary.get("section_number")
            section_id = section_map.get(section_number)
            if not section_id:
                continue
            _upsert_summary(
                db,
                section_id=str(section_id),
                summary_text=str(summary.get("summary_text") or ""),
                keywords=summary.get("keywords") or [],
                model_name=model_name,
            )
            stored += 1
        db.commit()
        return f"persisted:{stored}"
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Summary persistence failed: %s", exc)
        db.rollback()
        return f"failed:{exc}"
    finally:
        db.close()


def _fetch_section_ids(
    db: Any,
    document_version_id: str,
    sections: Sequence[SectionSpan],
) -> Dict[str, str]:
    numbers = [section.section_number for section in sections if section.section_number]
    if not numbers:
        return {}
    rows = (
        db.execute(
            sqltext(
                """
                SELECT id, section_number
                FROM document_sections
                WHERE document_version_id = :dvid
                  AND section_number = ANY(:sections)
                """
            ),
            {"dvid": document_version_id, "sections": numbers},
        )
        .mappings()
        .all()
    )
    return {row["section_number"]: str(row["id"]) for row in rows if row.get("section_number")}


def _upsert_summary(
    db: Any,
    *,
    section_id: str,
    summary_text: Any,
    keywords: Sequence[str],
    model_name: str,
) -> None:
    summary_json = _normalize_summary_json(summary_text, keywords)
    db.execute(
        sqltext(
            """
            DELETE FROM document_section_summary
            WHERE section_id = :sid
              AND summary_type = :stype
              AND summary_purpose = :purpose
            """
        ),
        {"sid": section_id, "stype": _SUMMARY_TYPE, "purpose": _SUMMARY_PURPOSE},
    )
    db.execute(
        sqltext(
            """
            INSERT INTO document_section_summary (
                section_id,
                summary_type,
                summary_purpose,
                summary_text,
                keywords,
                model_name,
                confidence
            )
            VALUES (
                :sid,
                :stype,
                :purpose,
                :summary_text,
                :keywords,
                :model_name,
                :confidence
            )
            """
        ),
        {
            "sid": section_id,
            "stype": _SUMMARY_TYPE,
            "purpose": _SUMMARY_PURPOSE,
            "summary_text": summary_json,
            "keywords": list(keywords),
            "model_name": model_name,
            "confidence": None,
        },
    )


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_MODULE = SimpleStageModule(
    StageModuleSpec(
        module_name="metadata-summary-extraction",
        env_prefix="METADATA_SUMMARY_EXTRACTION",
        description="Extract key metadata and summaries including anchors into source text.",
        default_queue_name="metadata-summary-extraction-queue",
    ),
    work_fn=_workload,
)


def handle_message(payload: Dict[str, object]) -> Dict[str, object]:
    """Delegate to the common stage handler for incoming payloads."""
    return _MODULE.handler(payload)


def run() -> None:
    """Launch the shared NotificationConsumer loop for this stage."""
    _MODULE.run()


__all__ = ["handle_message", "run"]
def _get_s3_client() -> Any:
    global _s3_client
    if _s3_client is None:
        if boto3 is None:
            raise RuntimeError("boto3 is required to access S3.")
        _s3_client = boto3.client("s3")
    return _s3_client
