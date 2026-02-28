# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Unit tests for pdf_analysis.ingest.tables."""

from __future__ import annotations

from pathlib import Path
import sys
import types

import pandas as pd

from pdf_analysis.ingest import tables


def test_normalise_page_selection() -> None:
    assert tables._normalise_page_selection(None, None) is None
    assert tables._normalise_page_selection([1, 3, 2, 0], None) == [1, 2, 3]
    assert tables._normalise_page_selection([1, 5], 3) == [1]
    assert tables._normalise_page_selection(None, 2) == [1, 2]


def test_tables_to_dfs_builds_headers() -> None:
    data = [["A", ""], ["1", "2"]]
    dfs = tables._tables_to_dfs([data])
    assert len(dfs) == 1
    df = dfs[0]
    assert list(df.columns) == ["A", "col_2"]
    assert df.iloc[0].tolist() == ["1", "2"]


def test_tables_to_dfs_recovers_header_after_title() -> None:
    data = [
        ["TABLE 1: TREATMENT GROUPS"],
        ["Group", "Diabetic", "Treatment", "Dose (mg/kg)"],
        ["1", "No", "PBS", "5 ml/kg"],
    ]
    dfs = tables._tables_to_dfs([data])
    assert len(dfs) == 1
    df = dfs[0]
    assert list(df.columns) == ["Group", "Diabetic", "Treatment", "Dose (mg/kg)"]
    assert df.iloc[0].tolist() == ["1", "No", "PBS", "5 ml/kg"]


def test_tables_to_dfs_merges_label_and_numeric_headers() -> None:
    data = [
        ["", "", "STUDY DAY", "", "", ""],
        ["Group", "Rat", "0", "28", "31", "38"],
        ["3", "22", "6.27", "13.69", "6.78", "8.12"],
    ]
    dfs = tables._tables_to_dfs([data])
    assert len(dfs) == 1
    df = dfs[0]
    assert list(df.columns) == [
        "Group",
        "Rat",
        "STUDY DAY 0",
        "STUDY DAY 28",
        "STUDY DAY 31",
        "STUDY DAY 38",
    ]
    assert df.iloc[0].tolist() == ["3", "22", "6.27", "13.69", "6.78", "8.12"]


def test_tables_to_dfs_skips_title_row_when_header_follows() -> None:
    data = [
        ["Title: Sample Study", "Page 1 of 2"],
        ["Group", "Treatment", "Dose (mg/kg)"],
        ["1", "PBS", "5"],
    ]
    dfs = tables._tables_to_dfs([data])
    assert len(dfs) == 1
    df = dfs[0]
    assert list(df.columns) == ["Group", "Treatment", "Dose (mg/kg)"]
    assert df.iloc[0].tolist() == ["1", "PBS", "5"]


def test_tables_to_dfs_skips_metadata_tables() -> None:
    data = [
        ["Title: Sample Study", "Page 1 of 2"],
        ["", ""],
        ["", ""],
    ]
    dfs = tables._tables_to_dfs([data])
    assert dfs == []

    data = [
        ["Title: Sample Study", "Page 1 of 2", "Sponsor"],
        ["This is a long sentence split", "across columns", "for layout"],
        ["More sentence text continues", "without numbers", "in columns"],
    ]
    dfs = tables._tables_to_dfs([data])
    assert dfs == []

    data = [
        ["Title: Sample Study", "Page 2 of 2", "Sponsor", ""],
        ["3 MATERIA", "LS AN", "D MET", "HOD", "S", "", ""],
        ["4 RESUL", "TS AN", "D DISC", "USSION", "", "", ""],
    ]
    dfs = tables._tables_to_dfs([data])
    assert dfs == []

    data = [
        ["Study Number: LT3114-PHA-001-R", "Version: 1.0"],
        ["Prepared By", "Sponsor"],
    ]
    dfs = tables._tables_to_dfs([data])
    assert dfs == []


def test_safe_extract_tables_marks_incompatible(monkeypatch) -> None:
    class FakePage:
        page_number = 1

        def extract_tables(self, _settings=None):
            raise AttributeError("graphicstate")

    tables._PDFPLUMBER_COMPATIBLE = True
    result = tables._safe_extract_tables(FakePage(), None)
    assert result == []
    assert tables._PDFPLUMBER_COMPATIBLE is False


def test_extract_tables_pdfplumber_stub(monkeypatch, tmp_path: Path) -> None:
    df = pd.DataFrame([{"A": 1}])

    def fake_plumber_tables_on_page(_page):
        return [df]

    tables_module = types.ModuleType("pdfplumber")

    class FakePDF:
        def __init__(self):
            self.pages = [object()]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_open(_path: str):
        return FakePDF()

    tables_module.open = fake_open  # type: ignore[attr-defined]
    monkeypatch.setattr(tables, "_plumber_tables_on_page", fake_plumber_tables_on_page)
    monkeypatch.setitem(sys.modules, "pdfplumber", tables_module)

    pdf_path = tmp_path / "dummy.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    result = tables.extract_tables(pdf_path, engine="pdfplumber")
    assert result
    entry = result[0]
    assert entry["page_number"] == 1
    assert entry["engine"] == "pdfplumber"
