# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Unit tests for pdf_analysis.export.persist helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from pdf_analysis.export import persist


def test_write_text_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    persist.write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_ensure_unique_columns_dedupes() -> None:
    df = pd.DataFrame([[1, 2, 3]], columns=["A", "A", None])
    fixed = persist.ensure_unique_columns(df)
    assert list(fixed.columns) == ["A", "A_2", "column"]


def test_ensure_unique_columns_no_change() -> None:
    df = pd.DataFrame([[1, 2]], columns=["A", "B"])
    fixed = persist.ensure_unique_columns(df)
    assert fixed is df


def test_save_tables_creates_outputs(tmp_path: Path) -> None:
    pdf_path = tmp_path / "input.pdf"
    pdf_path.write_text("stub", encoding="utf-8")
    df = pd.DataFrame([{"A": 1}, {"A": 2}])
    tables: List[Dict[str, Any]] = [
        {"page_number": 1, "index_on_page": 1, "engine": "pdfplumber", "dataframe": df}
    ]
    manifest = persist.save_tables(pdf_path, tables, tmp_path)
    assert manifest
    entry = manifest[0]
    assert (tmp_path / entry["csv"]).exists()
    assert (tmp_path / entry["json"]).exists()
    assert entry["preview_rows"].shape[0] == 2
