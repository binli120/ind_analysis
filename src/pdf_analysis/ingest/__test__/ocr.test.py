# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Unit tests for pdf_analysis.ingest.ocr."""

from __future__ import annotations

from pathlib import Path

from pdf_analysis.ingest import ocr


def test_ocr_pages_if_needed_no_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(ocr, "pytesseract", None)
    monkeypatch.setattr(ocr, "convert_from_path", None)
    result = ocr.ocr_pages_if_needed(Path("dummy.pdf"), [0])
    assert result == {}


def test_ocr_pages_if_needed_success(monkeypatch) -> None:
    class FakeTesseract:
        @staticmethod
        def image_to_string(_img, lang="eng") -> str:
            return f"text-{lang}"

    def fake_convert_from_path(*_args, **_kwargs):
        return [object(), object(), object()]

    monkeypatch.setattr(ocr, "pytesseract", FakeTesseract)
    monkeypatch.setattr(ocr, "convert_from_path", fake_convert_from_path)

    result = ocr.ocr_pages_if_needed(Path("dummy.pdf"), [0, 2], lang="eng")
    assert result == {0: "text-eng", 2: "text-eng"}


def test_ocr_pages_if_needed_pdfinfo_error(monkeypatch) -> None:
    class FakePDFInfoNotInstalledError(Exception):
        pass

    def fake_convert_from_path(*_args, **_kwargs):
        raise FakePDFInfoNotInstalledError("missing poppler")

    monkeypatch.setattr(ocr, "pytesseract", object())
    monkeypatch.setattr(ocr, "convert_from_path", fake_convert_from_path)
    monkeypatch.setattr(ocr, "PDFInfoNotInstalledError", FakePDFInfoNotInstalledError)

    result = ocr.ocr_pages_if_needed(Path("dummy.pdf"), [0])
    assert result == {}
