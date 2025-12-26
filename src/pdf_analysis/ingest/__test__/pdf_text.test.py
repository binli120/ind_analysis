# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Unit tests for pdf_analysis.ingest.pdf_text."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

from pdf_analysis.ingest import pdf_text


def test_iter_pages_text_selection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pages = [
        SimpleNamespace(label="Page 1"),
        SimpleNamespace(label="Page 2"),
        SimpleNamespace(label="Page 3"),
    ]

    monkeypatch.setattr(pdf_text, "_extract_page_text", lambda page, *_: page.label)
    monkeypatch.setattr(
        pdf_text.PDFPage,
        "get_pages",
        lambda *_args, **_kwargs: pages,
    )

    path = tmp_path / "dummy.pdf"
    path.write_bytes(b"%PDF-1.4")

    result = list(pdf_text.iter_pages_text(path, page_numbers=[2]))
    assert result == [{"page_number": "2", "text": "Page 2"}]


def test_iter_pages_text_respects_max_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pages = [SimpleNamespace(label=f"Page {idx}") for idx in range(1, 5)]
    monkeypatch.setattr(pdf_text, "_extract_page_text", lambda page, *_: page.label)
    monkeypatch.setattr(
        pdf_text.PDFPage,
        "get_pages",
        lambda *_args, **_kwargs: pages,
    )

    path = tmp_path / "dummy.pdf"
    path.write_bytes(b"%PDF-1.4")

    result = list(pdf_text.iter_pages_text(path, max_pages=2))
    assert len(result) == 2
    assert result[0]["page_number"] == "1"
    assert result[1]["page_number"] == "2"


def test_extract_pages_text_with_ocr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_iter_pages_text(_path, max_pages=None):
        _ = max_pages
        return iter(
            [
                {"page_number": "1", "text": ""},
                {"page_number": "2", "text": "text"},
            ]
        )

    monkeypatch.setattr(pdf_text, "iter_pages_text", fake_iter_pages_text)
    monkeypatch.setattr(pdf_text, "ocr_pages_if_needed", lambda *_args, **_kwargs: {0: "ocr"})

    path = tmp_path / "dummy.pdf"
    path.write_bytes(b"%PDF-1.4")

    pages = pdf_text.extract_pages_text(path, ocr_fallback=True)
    assert pages[0]["text"] == "ocr"
    assert pages[1]["text"] == "text"
