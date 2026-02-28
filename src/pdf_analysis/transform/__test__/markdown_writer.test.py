# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Unit tests for pdf_analysis.transform.markdown_writer."""

from __future__ import annotations

import pandas as pd

from pdf_analysis.transform import markdown_writer


def test_df_to_markdown_table() -> None:
    df = pd.DataFrame([{"A": 1, "B": "x"}])
    md = markdown_writer._df_to_markdown_table(df)
    assert "| A | B |" in md
    assert "| 1 | x |" in md


def test_text_to_html() -> None:
    html = markdown_writer._text_to_html("Line 1\n\nLine 2")
    assert "<p>Line 1</p>" in html
    assert "<p>Line 2</p>" in html


def test_build_markdown_document_includes_tables() -> None:
    pages = [{"page_number": 1, "text": "Body"}]
    table_manifest = [
        {
            "page_number": 1,
            "index_on_page": 1,
            "csv": "table.csv",
            "json": "table.json",
            "preview_rows": pd.DataFrame([{"A": "1"}]),
        }
    ]
    doc = markdown_writer.build_markdown_document("Doc", pages, table_manifest)
    assert "# Doc" in doc
    assert "Table (p1 t1)" in doc
    assert "[CSV](table.csv)" in doc


def test_build_markdown_document_accepts_preview_row_dicts() -> None:
    pages = [{"page_number": 1, "text": "Body"}]
    table_manifest = [
        {
            "page_number": 1,
            "index_on_page": 1,
            "csv": None,
            "json": None,
            "preview_rows": [{"A": "1"}],
        }
    ]
    doc = markdown_writer.build_markdown_document("Doc", pages, table_manifest)
    assert "| A |" in doc
    assert "| 1 |" in doc


def test_build_html_document_includes_tables() -> None:
    pages = [{"page_number": 1, "text": "Body"}]
    table_manifest = [
        {
            "page_number": 1,
            "index_on_page": 1,
            "csv": "table.csv",
            "json": "table.json",
            "preview_rows": pd.DataFrame([{"A": "1"}]),
        }
    ]
    doc = markdown_writer.build_html_document("Doc", pages, table_manifest)
    assert "<h1>Doc</h1>" in doc
    assert "Table (p1 t1)" in doc
    assert 'href="table.csv"' in doc


def test_build_html_document_accepts_preview_row_dicts() -> None:
    pages = [{"page_number": 1, "text": "Body"}]
    table_manifest = [
        {
            "page_number": 1,
            "index_on_page": 1,
            "csv": None,
            "json": None,
            "preview_rows": [{"A": "1"}],
        }
    ]
    doc = markdown_writer.build_html_document("Doc", pages, table_manifest)
    assert "<h1>Doc</h1>" in doc
    assert "<table" in doc
