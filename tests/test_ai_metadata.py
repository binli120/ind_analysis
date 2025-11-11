"""Tests covering the OpenAI metadata generation helper."""

from __future__ import annotations

import types

from pdf_analysis.service.ai_metadata import OpenAIMetadataGenerator


class DummyResponse:
    def __init__(self, text: str) -> None:
        self.output = [{"content": [{"text": text}]}]


class DummyClient:
    def __init__(self, text: str) -> None:
        self._response = DummyResponse(text)
        self.responses = types.SimpleNamespace(create=self._create)  # type: ignore[attr-defined]

    def _create(self, **_: object) -> DummyResponse:
        return self._response


def test_openai_metadata_generator_extracts_ind_classification() -> None:
    payload = (
        '{"labels":["module 1"],"keywords":["cover letter"],"language":"en",'
        '"ind_document_type":"cover letter","ind_section_number":"1.2",'
        '"ind_section_title":"Cover Letters","ind_confidence":0.78}'
    )

    generator = OpenAIMetadataGenerator()
    generator._client = DummyClient(payload)  # type: ignore[attr-defined]

    result = generator("Sample IND cover letter content referencing FDA Form 1571.")

    assert result["labels"] == ["module 1"]
    assert result["keywords"] == ["cover letter"]
    assert result["language"] == "en"
    assert result["ind_document_type"] == "cover letter"
    assert result["ind_section_number"] == "1.2"
    assert result["ind_section_title"] == "Cover Letters"
    assert result["ind_classification_confidence"] == 0.78
    assert result["analyzed"] is True
