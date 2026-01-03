# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Utility helpers for persisting extracted artefacts to disk."""

# @author: Bin Lee
# @email: blee@filynai.com

from pathlib import Path
from typing import Any, Dict, List

import pandas as pd


def write_text(path: Path, text: str) -> None:
    """Persist plain text to disk using UTF-8 encoding."""
    path.write_text(text, encoding="utf-8")


def ensure_unique_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with unique column names to avoid record-orient errors."""
    columns = [str(c) if c is not None else "" for c in df.columns]
    counts: dict[str, int] = {}
    unique_cols: list[str] = []
    for col in columns:
        base = col or "column"
        count = counts.get(base, 0) + 1
        counts[base] = count
        if count == 1:
            unique_cols.append(base)
        else:
            unique_cols.append(f"{base}_{count}")
    if unique_cols == columns:
        return df
    fixed = df.copy()
    fixed.columns = unique_cols
    return fixed


def save_tables(
    pdf_path: Path, tables: List[Dict[str, Any]], outdir: Path
) -> List[Dict[str, Any]]:
    """
    Saves each table as CSV and JSON, returns manifest with preview rows for
    Markdown.
    """
    manifest: List[Dict[str, Any]] = []
    stem = Path(pdf_path).stem
    for t in tables:
        pno = t["page_number"]
        idx = t["index_on_page"]
        df: pd.DataFrame = t["dataframe"]

        # Clean up column names/values
        df = df.fillna("").astype(str)
        df = ensure_unique_columns(df)

        csv_path = outdir / f"{stem}.p{pno}.t{idx}.csv"
        json_path = outdir / f"{stem}.p{pno}.t{idx}.json"
        df.to_csv(csv_path, index=False)
        df.to_json(json_path, orient="records", force_ascii=False, indent=2)

        manifest.append(
            {
                "page_number": pno,
                "index_on_page": idx,
                "engine": t["engine"],
                "csv": csv_path.name,
                "json": json_path.name,
                "preview_rows": df.head(10),  # embed top-10 as markdown table
            }
        )
    return manifest
