"""Build CTD 2.4 element references from template + Module 4 data."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from ncd.ctd_materials import (
    fetch_assets_for_sections,
    fetch_key_sections_for_sections,
    fetch_project_name,
    fetch_section_sources,
    module4_sections_for_ctd_targets,
    resolve_ctd_targets,
)
from ncd.ctd_template import load_template_entries, normalize_element_number
from ncd.extraction.fast_extract import parse_fast_method


FAST_FIELD = "Fast extraction method (keywords/tables/regex)"
_MODULE4_RE = re.compile(r"\b4\.\d+(?:\.\d+){0,2}\b")


def find_template_entry(element_number: str) -> Optional[Dict[str, Any]]:
    target = normalize_element_number(element_number)
    if not target:
        return None
    for entry in load_template_entries():
        raw = str(entry.get("Subsection Element Numbering") or "").strip()
        if normalize_element_number(raw) == target:
            return entry
    return None


def list_template_elements(section_prefix: str | None = "2.4.") -> List[str]:
    elements: List[str] = []
    for entry in load_template_entries():
        raw = str(entry.get("Subsection Element Numbering") or "").strip()
        normalized = normalize_element_number(raw)
        if not normalized:
            continue
        if section_prefix and not normalized.startswith(section_prefix):
            continue
        elements.append(normalized)
    return _dedupe_terms(elements)


def build_ctd_element_reference(
    db: Session,
    *,
    tenant_id: str,
    project_id: str,
    bucket: str,
    element_number: str,
    include_assets: bool = True,
    include_key_sections: bool = True,
) -> Dict[str, Any]:
    entry = find_template_entry(element_number)
    if not entry:
        raise ValueError("element not found in template")

    normalized = normalize_element_number(element_number)
    section_number = (
        str(entry.get("Subsection") or entry.get("Section") or "").strip()
    )
    module4_sections = _extract_module4_sections(entry)
    mapping_entries: List[Dict[str, Any]] = []
    targets: List[str] = []
    if not module4_sections and section_number:
        module4_sections, mapping_entries, targets = module4_sections_for_ctd_targets(
            section_number
        )
    elif section_number:
        targets = resolve_ctd_targets(section_number)

    project_name = fetch_project_name(db, project_id)
    project_like = f"%/{project_name}/%" if project_name else "%"

    sources = fetch_section_sources(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
        module4_sections=module4_sections,
    )

    keywords, table_cues, regex_patterns = parse_fast_method(
        str(entry.get(FAST_FIELD) or "")
    )
    fast_keywords = _dedupe_terms([*keywords, *table_cues])
    has_filters = bool(fast_keywords or regex_patterns)

    source_payload = _filter_sources(
        sources, fast_keywords, regex_patterns, has_filters=has_filters
    )

    assets_payload: List[Dict[str, Any]] = []
    if include_assets:
        assets = fetch_assets_for_sections(
            db,
            tenant_id=tenant_id,
            bucket=bucket,
            project_like=project_like,
            module4_sections=module4_sections,
            asset_type=None,
        )
        assets_payload = _filter_assets(
            assets, fast_keywords, regex_patterns, has_filters=has_filters
        )

    key_sections_payload: List[Dict[str, Any]] = []
    if include_key_sections:
        key_sections = fetch_key_sections_for_sections(
            db,
            tenant_id=tenant_id,
            bucket=bucket,
            project_like=project_like,
            module4_sections=module4_sections,
            section_type=None,
        )
        key_sections_payload = _filter_key_sections(
            key_sections, fast_keywords, regex_patterns, has_filters=has_filters
        )

    return {
        "element_number": normalized,
        "section_number": section_number,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "bucket": bucket,
        "module4_sections": module4_sections,
        "ctd_targets": targets,
        "mapping": mapping_entries,
        "template": entry,
        "fast_extract": {
            "keywords": fast_keywords,
            "regex": regex_patterns,
        },
        "sources": source_payload,
        "assets": assets_payload,
        "key_sections": key_sections_payload,
    }


def _extract_module4_sections(entry: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []
    for field in (
        entry.get("Module 4 source (exact report + section/table)"),
        entry.get("Data needed to write this overview element (inputs)"),
        entry.get("Content"),
    ):
        if not isinstance(field, str):
            continue
        candidates.extend(_MODULE4_RE.findall(field))
    return sorted(_dedupe_terms(candidates))


def _dedupe_terms(items: Iterable[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for item in items:
        cleaned = str(item).strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(cleaned)
    return ordered


def _filter_sources(
    sources: Sequence[Dict[str, Any]],
    keywords: Sequence[str],
    regex_patterns: Sequence[str],
    *,
    has_filters: bool,
) -> List[Dict[str, Any]]:
    payload: List[Dict[str, Any]] = []
    for row in sources:
        summary = _normalize_summary(row.get("summary_text"))
        summary_text = ""
        if isinstance(summary, str):
            summary_text = summary
        elif summary is not None:
            summary_text = json.dumps(summary, ensure_ascii=True)
        combined = _combine_text(
            row.get("section_title"),
            row.get("section_number"),
            summary_text,
            " ".join(row.get("keywords") or []),
        )
        matched_keywords, matched_regex = _match_terms(
            combined, keywords, regex_patterns, row_keywords=row.get("keywords") or []
        )
        if has_filters and not (matched_keywords or matched_regex):
            continue
        payload.append(
            {
                "section_number": row.get("section_number"),
                "section_title": row.get("section_title"),
                "summary": summary,
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
                "s3": _build_s3_payload(row),
                "match": {
                    "keywords": matched_keywords,
                    "regex": matched_regex,
                },
            }
        )
    return payload


def _filter_assets(
    assets: Sequence[Dict[str, Any]],
    keywords: Sequence[str],
    regex_patterns: Sequence[str],
    *,
    has_filters: bool,
) -> List[Dict[str, Any]]:
    payload: List[Dict[str, Any]] = []
    for row in assets:
        combined = _combine_text(
            row.get("caption"),
            row.get("description"),
            " ".join(row.get("keywords") or []),
        )
        matched_keywords, matched_regex = _match_terms(combined, keywords, regex_patterns)
        if has_filters and not (matched_keywords or matched_regex):
            continue
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
                "extra_attributes": _normalize_extra(row.get("extra_attributes")),
                "document_version_id": row.get("document_version_id"),
                "document_s3_key": row.get("document_s3_key"),
                "match": {
                    "keywords": matched_keywords,
                    "regex": matched_regex,
                },
            }
        )
    return payload


def _filter_key_sections(
    sections: Sequence[Dict[str, Any]],
    keywords: Sequence[str],
    regex_patterns: Sequence[str],
    *,
    has_filters: bool,
) -> List[Dict[str, Any]]:
    payload: List[Dict[str, Any]] = []
    for row in sections:
        asset_ids = [str(a) for a in (row.get("asset_ids") or [])]
        combined = _combine_text(
            row.get("text"),
            " ".join(asset_ids),
        )
        matched_keywords, matched_regex = _match_terms(combined, keywords, regex_patterns)
        if has_filters and not (matched_keywords or matched_regex):
            continue
        payload.append(
            {
                "id": row.get("id"),
                "section_type": row.get("section_type"),
                "text": row.get("text"),
                "page_start": row.get("page_start"),
                "page_end": row.get("page_end"),
                "char_start": row.get("char_start"),
                "char_end": row.get("char_end"),
                "asset_ids": row.get("asset_ids") or [],
                "model_name": row.get("model_name"),
                "confidence": row.get("confidence"),
                "document_version_id": row.get("document_version_id"),
                "s3_bucket": row.get("s3_bucket"),
                "document_s3_key": row.get("document_s3_key"),
                "match": {
                    "keywords": matched_keywords,
                    "regex": matched_regex,
                },
            }
        )
    return payload


def _build_s3_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    bucket = row.get("s3_bucket")
    key = row.get("s3_key")
    markdown_key = f"{key}.extracted.md" if key else None
    return {
        "bucket": bucket,
        "key": key,
        "version_id": row.get("s3_version_id"),
        "pdf_s3_uri": f"s3://{bucket}/{key}" if bucket and key else None,
        "markdown_s3_uri": f"s3://{bucket}/{markdown_key}" if bucket and markdown_key else None,
    }


def _normalize_summary(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return value


def _normalize_extra(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _combine_text(*parts: Any) -> str:
    return " ".join(str(part or "") for part in parts if part).strip()


def _match_terms(
    text: str,
    keywords: Sequence[str],
    regex_patterns: Sequence[str],
    *,
    row_keywords: Sequence[str] | None = None,
) -> Tuple[List[str], List[str]]:
    if not text:
        return [], []
    lowered = text.lower()
    matched_keywords = [kw for kw in keywords if kw.lower() in lowered]
    if row_keywords:
        lowered_row = [str(k).lower() for k in row_keywords]
        for kw in keywords:
            if kw.lower() in lowered_row and kw not in matched_keywords:
                matched_keywords.append(kw)
    matched_regex = [pat for pat in regex_patterns if _regex_hit(pat, text)]
    return matched_keywords, matched_regex


def _regex_hit(pattern: str, text: str) -> bool:
    try:
        return re.search(pattern, text, re.IGNORECASE) is not None
    except re.error:
        return False
