# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Unit tests for pdf_analysis.export.pdf_writer."""

from __future__ import annotations

from pathlib import Path
import sys
import types

import pytest

from pdf_analysis.export import pdf_writer


def test_markdown_to_pdf_requires_reportlab(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_import(name: str, *args, **kwargs):  # type: ignore[override]
        if name.startswith("reportlab"):
            raise ImportError("reportlab missing")
        return original_import(name, *args, **kwargs)

    original_import = __import__
    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(RuntimeError):
        pdf_writer.markdown_to_pdf("text", tmp_path / "out.pdf")


def test_markdown_to_pdf_writes_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reportlab = types.ModuleType("reportlab")
    lib = types.ModuleType("reportlab.lib")
    pagesizes = types.ModuleType("reportlab.lib.pagesizes")
    units = types.ModuleType("reportlab.lib.units")
    pdfgen = types.ModuleType("reportlab.pdfgen")
    canvas_mod = types.ModuleType("reportlab.pdfgen.canvas")

    pagesizes.letter = (100, 100)
    units.inch = 10

    class FakeCanvas:
        def __init__(self, path: str, pagesize=None):
            self.path = path
            self.pagesize = pagesize
            self.lines: list[str] = []

        def drawString(self, _x, _y, line):
            self.lines.append(line)

        def showPage(self):
            return None

        def save(self):
            Path(self.path).write_bytes(b"%PDF-1.4\n%%EOF")

    canvas_mod.Canvas = FakeCanvas

    sys.modules.update(
        {
            "reportlab": reportlab,
            "reportlab.lib": lib,
            "reportlab.lib.pagesizes": pagesizes,
            "reportlab.lib.units": units,
            "reportlab.pdfgen": pdfgen,
            "reportlab.pdfgen.canvas": canvas_mod,
        }
    )

    output = tmp_path / "out.pdf"
    pdf_writer.markdown_to_pdf("Line 1\n\nLine 2", output)
    assert output.exists()
