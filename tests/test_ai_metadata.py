# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Tests covering the OpenAI metadata generation helper."""

from __future__ import annotations

import types

from pdf_analysis.constants.metadata_keys import (
    ANALYZED_KEY,
    IND_CLASSIFICATION_CONFIDENCE_KEY,
    IND_DOCUMENT_TYPE_KEY,
    IND_SECTION_NUMBER_KEY,
    IND_SECTION_TITLE_KEY,
    KEYWORDS_KEY,
    LABELS_KEY,
    LANGUAGE_KEY,
)
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
        f'{{"{LABELS_KEY}":["module 1"],"{KEYWORDS_KEY}":["cover letter"],'
        f'"{LANGUAGE_KEY}":"en","{IND_DOCUMENT_TYPE_KEY}":"cover letter",'
        f'"{IND_SECTION_NUMBER_KEY}":"1.2","{IND_SECTION_TITLE_KEY}":"Cover Letters",'
        '"ind_confidence":0.78}'
    )

    generator = OpenAIMetadataGenerator()
    generator._client = DummyClient(payload)  # type: ignore[attr-defined]

    result = generator("Sample IND cover letter content referencing FDA Form 1571.")

    assert result[LABELS_KEY] == ["module 1"]
    assert result[KEYWORDS_KEY] == ["cover letter"]
    assert result[LANGUAGE_KEY] == "en"
    assert result[IND_DOCUMENT_TYPE_KEY] == "cover letter"
    assert result[IND_SECTION_NUMBER_KEY] == "1.2"
    assert result[IND_SECTION_TITLE_KEY] == "Cover Letters"
    assert result[IND_CLASSIFICATION_CONFIDENCE_KEY] == 0.78
    assert result[ANALYZED_KEY] is True
