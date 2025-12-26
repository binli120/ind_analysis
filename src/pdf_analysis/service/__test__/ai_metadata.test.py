# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Unit tests for pdf_analysis.service.ai_metadata."""

from __future__ import annotations

from typing import Any, Dict

import types

from pdf_analysis.service import ai_metadata


def test_ensure_list_dedupes_and_limits() -> None:
    values = ["Alpha", "alpha", "Beta", "Gamma", "Delta"]
    assert ai_metadata._ensure_list(values, limit=3) == ["Alpha", "Beta", "Gamma"]


def test_strip_code_fence_json() -> None:
    text = "```json\n{\"a\": 1}\n```"
    assert ai_metadata._strip_code_fence(text) == "{\"a\": 1}"


def test_collect_text_fragments_handles_dict() -> None:
    content = [{"text": {"value": "hello"}}]
    assert ai_metadata._collect_text_fragments(content) == ["hello"]


def test_extract_text_from_response_model_dump() -> None:
    class FakeResponse:
        def model_dump(self) -> Dict[str, Any]:
            return {"output": [{"content": [{"text": {"value": "hi"}}]}]}

    assert ai_metadata._extract_text_from_response(FakeResponse()) == "hi"


def test_openai_metadata_generator_no_client(monkeypatch) -> None:
    monkeypatch.setattr(ai_metadata, "OpenAI", None)
    generator = ai_metadata.OpenAIMetadataGenerator()
    assert generator._client is None
    assert generator("text") == {}
