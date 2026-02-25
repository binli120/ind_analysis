"""Shared request/validation helpers for API routes."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Sequence

from fastapi import HTTPException

from pdf_analysis.api.constants import USER_PROMPT_MAX_CHARS


def _normalize_user_prompt(value: Optional[str]) -> Optional[str]:
    """Normalize and bound user-supplied prompt text before prompt assembly."""
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) > USER_PROMPT_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"user_prompt exceeds {USER_PROMPT_MAX_CHARS} characters.",
        )
    return normalized


def _normalize_json_dict(value: Any) -> Dict[str, Any]:
    """Normalize json dict."""
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
    """Require uuid."""
    if not value:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a UUID")
    return value


def _normalize_optional_uuid(value: Optional[str], field_name: str) -> Optional[str]:
    """Normalize optional uuid."""
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
    """Normalize uuid list."""
    normalized: List[str] = []
    for value in values:
        if value is None:
            continue
        try:
            normalized.append(str(uuid.UUID(str(value))))
        except (ValueError, TypeError):
            continue
    return normalized
