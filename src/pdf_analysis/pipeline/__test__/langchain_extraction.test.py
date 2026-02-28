# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Unit tests for pdf_analysis.pipeline.langchain_extraction."""

from __future__ import annotations

from typing import Any, Dict, List

from pdf_analysis.pipeline.langchain_extraction import LangChainExtractionPipeline
from pdf_analysis.pipeline.pipeline import DocumentChunk


class FakeChain:
    def __init__(self, payload: Any):
        self.payload = payload

    def invoke(self, _input: Dict[str, Any]):
        return self.payload


class FakeRepo:
    pass


def test_normalize_list_handles_dict() -> None:
    pipeline = LangChainExtractionPipeline(
        study_segmentation_chain=FakeChain({"studies": []}),
        noael_chain=FakeChain({"items": []}),
        repository=FakeRepo(),
    )
    assert pipeline._normalize_list({"items": [{"a": 1}]}, field_name="items") == [
        {"a": 1}
    ]
    assert pipeline._normalize_list({"a": 1}, field_name=None) == [{"a": 1}]


def test_composite_confidence_weights() -> None:
    pipeline = LangChainExtractionPipeline(
        study_segmentation_chain=FakeChain({"studies": []}),
        noael_chain=FakeChain({"items": []}),
        repository=FakeRepo(),
    )
    confidence = pipeline._composite_confidence({"llm_confidence": 0.8})
    assert confidence == 0.8
    assert pipeline._composite_confidence({}) is None


def test_render_chunks_for_prompt() -> None:
    pipeline = LangChainExtractionPipeline(
        study_segmentation_chain=FakeChain({"studies": []}),
        noael_chain=FakeChain({"items": []}),
        repository=FakeRepo(),
    )
    chunks = [
        DocumentChunk(
            chunk_id="c1", page=1, text="Text", bbox=None, offset_start=0, offset_end=4
        )
    ]
    rendered = pipeline._render_chunks_for_prompt(chunks)
    assert "[page=1" in rendered
    assert "Text" in rendered


def test_segment_studies_parses_payload() -> None:
    response = {
        "studies": [
            {
                "study_id": "s1",
                "study_type": "tox",
                "start_page": 1,
                "end_page": 2,
            }
        ]
    }
    pipeline = LangChainExtractionPipeline(
        study_segmentation_chain=FakeChain(response),
        noael_chain=FakeChain({"items": []}),
        repository=FakeRepo(),
    )
    segments = pipeline.segment_studies(
        [
            DocumentChunk(
                chunk_id="c", page=1, text="t", bbox=None, offset_start=0, offset_end=1
            )
        ]
    )
    assert len(segments) == 1
    assert segments[0].study_id == "s1"


def test_extract_noael_returns_results() -> None:
    response = {
        "items": [
            {
                "dose": 10,
                "dose_unit": "mg/kg",
                "species": "rat",
                "sex": "F",
                "endpoint": "tox",
                "quote": "NOAEL",
                "confidence": 0.9,
            }
        ]
    }
    pipeline = LangChainExtractionPipeline(
        study_segmentation_chain=FakeChain({"studies": []}),
        noael_chain=FakeChain(response),
        repository=FakeRepo(),
    )
    chunk = DocumentChunk(
        chunk_id="c1", page=1, text="Text", bbox=None, offset_start=0, offset_end=4
    )
    results = pipeline.extract_noael([chunk])
    assert len(results) == 1
    record, returned_chunk = results[0]
    assert record.dose == 10
    assert returned_chunk is chunk


def test_extract_pk_returns_results() -> None:
    response = {
        "items": [
            {
                "parameter": "Cmax",
                "value": 2.0,
                "unit": "ng/mL",
                "dose_group": "high",
                "quote": "Cmax",
                "confidence": 0.8,
            }
        ]
    }
    pipeline = LangChainExtractionPipeline(
        study_segmentation_chain=FakeChain({"studies": []}),
        noael_chain=FakeChain({"items": []}),
        pk_chain=FakeChain(response),
        repository=FakeRepo(),
    )
    chunk = DocumentChunk(
        chunk_id="c1", page=1, text="Text", bbox=None, offset_start=0, offset_end=4
    )
    results = pipeline.extract_pk([chunk])
    assert len(results) == 1
    record, returned_chunk = results[0]
    assert record.parameter == "Cmax"
    assert returned_chunk is chunk
