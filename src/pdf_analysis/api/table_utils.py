"""Helpers for table payload shaping and prompt-safe previews."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd


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
                "preview_rows": df.head(preview_rows).to_dict(orient="records"),
            }
        )
    return manifest


def _parse_columns_header(value: Any) -> List[str]:
    """Parse columns header."""
    if not isinstance(value, str):
        return []
    separator = ";"
    if ";" not in value and "|" in value:
        separator = "|"
    return [item.strip() for item in value.split(separator) if item.strip()]


def _truncate_preview_value(value: Any, max_len: int = 300) -> str:
    """Truncate preview value."""
    text = str(value) if value is not None else ""
    if len(text) <= max_len:
        return text
    return f"{text[:max_len].rstrip()}..."
