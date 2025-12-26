# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Unit tests for pdf_analysis.validate.quality."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from pdf_analysis.validate import quality


def test_normalize_identifier() -> None:
    assert quality._normalize_identifier("Report-123") == "report123"


def test_extract_int() -> None:
    assert quality._extract_int("Pages: 12") == 12
    assert quality._extract_int("No digits") is None


def test_parse_date() -> None:
    assert quality._parse_date("2024-01-31")
    assert quality._parse_date("") is None


def test_is_generic_header() -> None:
    assert quality._is_generic_header(None) is True
    assert quality._is_generic_header("col_1") is True
    assert quality._is_generic_header("Header") is False


def test_extract_key_values_simple() -> None:
    df = pd.DataFrame([{"A": "Report Number", "B": "123"}, {"A": "Pages", "B": "2"}])
    kv = quality._extract_key_values(df)
    assert kv["Report Number"] == "123"
    assert kv["Pages"] == "2"


def test_missing_cell_count() -> None:
    df = pd.DataFrame([[None, ""], ["x", "y"]])
    assert quality._missing_cell_count(df) == 2


def test_generate_quality_report_flags_mismatch(tmp_path: Path) -> None:
    pdf_path = tmp_path / "report-1.pdf"
    pages = [{"page_number": "1", "text": "text"}]
    df = pd.DataFrame([{"Key": "Report Number", "Value": "XYZ"}, {"Key": "Number of Pages", "Value": "2"}])
    tables = [{"page_number": 1, "index_on_page": 1, "dataframe": df}]

    report = quality.generate_quality_report(pdf_path, pages, tables)
    issues = report["json"]["issues"]
    assert any("Report number" in issue["message"] for issue in issues)
    assert any("Declared number of pages" in issue["message"] for issue in issues)
