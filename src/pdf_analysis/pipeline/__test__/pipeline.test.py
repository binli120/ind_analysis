# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Unit tests for pdf_analysis.pipeline.pipeline helpers."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

import re

from pdf_analysis.pipeline.pipeline import PDFProcessingPipeline, PipelineChunk, PipelineContext


def test_pipeline_chunk_to_serializable() -> None:
    df = pd.DataFrame([{"A": 1}])
    chunk = PipelineChunk(
        chunk_index=1,
        pages=[{"page_number": "1", "text": "hello"}],
        tables=[{"page_number": 1, "index_on_page": 1, "engine": "pdfplumber", "dataframe": df}],
        ocr_pages=[1],
        text_engine="pdfplumber",
        ocr_strategy=None,
        table_engines=["pdfplumber"],
    )
    payload = chunk.to_serializable()
    assert payload["tables"][0]["columns"] == ["A"]
    assert payload["tables"][0]["rows"] == [{"A": 1}]


def test_split_by_headings() -> None:
    pipeline = PDFProcessingPipeline()
    text = "1.1 Heading\nLine A\n1.2 Next\nLine B"
    pattern = re.compile(r"^\s*(\d+(\.\d+)+\s+)")
    segments = pipeline._split_by_headings(text, pattern)
    assert len(segments) == 2
    assert segments[0].startswith("1.1")
    assert segments[1].startswith("1.2")


def test_build_document_chunks_includes_tables() -> None:
    pipeline = PDFProcessingPipeline()
    ctx = PipelineContext(
        pdf_path=Path("dummy.pdf"),
        pages=[
            {"page_number": "1", "text": "1.1 Heading\nLine A"},
            {"page_number": "2", "text": ""},
        ],
        tables=[
            {
                "page_number": 1,
                "index_on_page": 1,
                "engine": "pdfplumber",
                "dataframe": pd.DataFrame([{"A": "x"}]),
            }
        ],
    )
    chunks = pipeline._build_document_chunks(ctx)
    assert len(chunks) == 2
    assert any(chunk.text.startswith("1.1") for chunk in chunks)
    assert any("A" in chunk.text and "x" in chunk.text for chunk in chunks)


def test_compute_metrics_populates_quality() -> None:
    pipeline = PDFProcessingPipeline()
    ctx = PipelineContext(
        pdf_path=Path("dummy.pdf"),
        pages=[{"page_number": "1", "text": "text"}, {"page_number": "2", "text": ""}],
        tables=[{"page_number": 1}],
        key_values={"k": "v"},
    )
    quality = {"json": {}}
    metrics = pipeline._compute_metrics(ctx, quality)
    assert metrics.total_pages == 2
    assert metrics.pages_with_text == 1
    assert quality["json"]["pipeline_metrics"]["tables_total"] == 1
