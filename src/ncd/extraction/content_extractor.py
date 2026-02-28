# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""LLM helpers for contextual content extraction."""

# @author: Bin Lee
# @email: blee@longooc.com

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence

from ncd.llm.llm_client import LLMClient

KEY_SECTION_MIN_CHARS = int(os.getenv("KEY_SECTION_MIN_CHARS", "300"))
KEY_SECTION_MAX_CHARS = int(os.getenv("KEY_SECTION_MAX_CHARS", "4000"))
ASSET_CONTEXT_MAX_CHARS = int(os.getenv("ASSET_CONTEXT_MAX_CHARS", "1200"))
ASSET_KEYWORDS_MIN = int(os.getenv("ASSET_KEYWORDS_MIN", "5"))
ASSET_KEYWORDS_MAX = int(os.getenv("ASSET_KEYWORDS_MAX", "10"))


def _clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0]


def _clean_keywords(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    keywords = []
    for item in raw:
        value = str(item or "").strip()
        if not value:
            continue
        if value.lower() in {"n/a", "none", "null"}:
            continue
        keywords.append(value)
    deduped = []
    seen = set()
    for kw in keywords:
        key = kw.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(kw)
    return deduped[:ASSET_KEYWORDS_MAX]


def _pad_keywords(keywords: List[str], candidates: Sequence[str]) -> List[str]:
    if len(keywords) >= ASSET_KEYWORDS_MIN:
        return keywords
    seen = {kw.lower() for kw in keywords}
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        keywords.append(value)
        seen.add(key)
        if len(keywords) >= ASSET_KEYWORDS_MIN:
            break
    return keywords[:ASSET_KEYWORDS_MAX]


def _find_excerpt_offset(text: str, excerpt: str) -> Optional[int]:
    if not excerpt:
        return None
    idx = text.find(excerpt)
    if idx != -1:
        return idx
    lower = text.lower()
    lower_excerpt = excerpt.lower()
    idx = lower.find(lower_excerpt)
    if idx != -1:
        return idx
    return None


def extract_key_sections_from_pages(
    llm: LLMClient,
    pages: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return summary/conclusion passages detected on each page."""
    results: List[Dict[str, Any]] = []
    system_prompt = (
        "You are a nonclinical regulatory analyst. Identify any passages that are "
        "summaries or conclusions within the provided page text. Return JSON with "
        'key "matches" as a list of objects: {"type": "summary"|"conclusion", '
        '"excerpt": "<exact substring from input>", "confidence": 0-1}. '
        "Use exact quotes from the input text and do not paraphrase. "
        'If no summary or conclusion text exists, return {"matches": []}.'
    )

    for page in pages:
        page_number = page.get("page_number")
        text = str(page.get("text") or "").strip()
        if len(text) < KEY_SECTION_MIN_CHARS:
            continue
        snippet = _clip_text(text, KEY_SECTION_MAX_CHARS)
        user_prompt = f'Page {page_number} text:\n"""\n{snippet}\n"""'
        try:
            payload = llm.extract_json(system_prompt, user_prompt)
        except Exception:
            continue
        matches = payload.get("matches") if isinstance(payload, dict) else None
        if not isinstance(matches, list):
            continue
        for item in matches:
            if not isinstance(item, dict):
                continue
            section_type = str(item.get("type") or "").strip().lower()
            if section_type not in {"summary", "conclusion"}:
                continue
            excerpt = str(item.get("excerpt") or "").strip()
            if not excerpt:
                continue
            offset = _find_excerpt_offset(text, excerpt)
            record = {
                "section_type": section_type,
                "text": excerpt,
                "page_start": page_number,
                "page_end": page_number,
                "char_start": offset,
                "char_end": offset + len(excerpt) if offset is not None else None,
                "confidence": item.get("confidence"),
            }
            results.append(record)

    return results


def describe_table_asset(
    llm: LLMClient,
    *,
    page_number: int | None,
    index_on_page: int | None,
    columns: Sequence[str],
    preview_rows: Sequence[Dict[str, Any]],
    page_text: str,
) -> Dict[str, Any]:
    system_prompt = (
        "You are a nonclinical regulatory analyst. Summarize the table content in 1-2 sentences "
        "and provide 5-10 domain-specific keywords. Return JSON with keys "
        '"description" (string) and "keywords" (array). Do not invent values.'
    )
    snippet = _clip_text(page_text.strip(), ASSET_CONTEXT_MAX_CHARS)
    context = {
        "page_number": page_number,
        "table_index": index_on_page,
        "columns": list(columns),
        "preview_rows": list(preview_rows),
        "page_text": snippet,
    }
    user_prompt = f"Table context:\n{json.dumps(context, ensure_ascii=True)}"
    payload = llm.extract_json(system_prompt, user_prompt)
    description = str(payload.get("description") or "").strip()
    keywords = _clean_keywords(payload.get("keywords"))
    keywords = _pad_keywords(keywords, [str(c) for c in columns])
    return {
        "description": description,
        "keywords": keywords,
    }


def describe_image_asset(
    llm: LLMClient,
    *,
    page_number: int | None,
    index_on_page: int | None,
    caption: str | None,
    page_text: str,
) -> Dict[str, Any]:
    system_prompt = (
        "You are a nonclinical regulatory analyst. Using the caption and surrounding text, "
        "describe the image in 1-2 sentences and provide 5-10 domain-specific keywords. "
        'Return JSON with keys "description" and "keywords". Do not infer details beyond the text.'
    )
    snippet = _clip_text(page_text.strip(), ASSET_CONTEXT_MAX_CHARS)
    context = {
        "page_number": page_number,
        "image_index": index_on_page,
        "caption": caption or "",
        "page_text": snippet,
    }
    user_prompt = f"Image context:\n{json.dumps(context, ensure_ascii=True)}"
    payload = llm.extract_json(system_prompt, user_prompt)
    description = str(payload.get("description") or "").strip()
    keywords = _clean_keywords(payload.get("keywords"))
    keywords = _pad_keywords(keywords, [caption or "image"])
    return {
        "description": description,
        "keywords": keywords,
    }
