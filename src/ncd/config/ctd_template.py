# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Helpers for loading IND 2.4/2.6 template entries."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List


_ELEMENT_RE = re.compile(r"^(2\.4(?:\.\d+)*)(?:-)?([a-z])$", re.IGNORECASE)


def normalize_element_number(value: str | None) -> str:
    if not value:
        return ""
    cleaned = value.strip().lower().replace(" ", "")
    match = _ELEMENT_RE.match(cleaned)
    if match:
        return f"{match.group(1)}-{match.group(2).lower()}"
    return cleaned


def _template_paths() -> List[Path]:
    base = Path(__file__).resolve().parents[2]
    return [base / "ncd" / "ind_24_26_template.json"]


def resolve_template_path() -> Path | None:
    """Return the configured IND template path if available."""
    for path in _template_paths():
        if path.exists():
            return path
    return None


def load_template_entries() -> List[Dict[str, Any]]:
    template_path = resolve_template_path()
    if not template_path:
        return []

    payload = json.loads(template_path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        entries: List[Dict[str, Any]] = []
        for value in payload.values():
            if isinstance(value, list):
                entries.extend(item for item in value if isinstance(item, dict))
        return entries
    return []
