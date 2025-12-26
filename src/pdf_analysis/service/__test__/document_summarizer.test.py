# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Unit tests for pdf_analysis.service.document_summarizer."""

from __future__ import annotations

from typing import List

import types

from pdf_analysis.service import document_summarizer


def test_slugify() -> None:
    assert document_summarizer._slugify("Hello World") == "hello-world"


def test_format_summary_text() -> None:
    summary = document_summarizer.TopicSummaryResult(
        summary="Summary text",
        topics=[
            document_summarizer.TopicSection(title="Topic", description="Desc", anchor="topic")
        ],
    )
    output = document_summarizer.format_summary_text(summary)
    assert "Document Summary" in output
    assert "Key Topics" in output
    assert "[Topic](#topic)" in output


def test_embed_topics_into_markdown() -> None:
    topics = [document_summarizer.TopicSection(title="Alpha", description="", anchor="alpha")]
    markdown = "# Title\n\nAlpha section"
    updated = document_summarizer.embed_topics_into_markdown(markdown, topics)
    assert "## Key Topics" in updated
    assert '<a id="alpha"></a>' in updated


def test_openai_document_summarizer_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(document_summarizer, "OpenAI", None)
    summarizer = document_summarizer.OpenAIDocumentSummarizer()
    assert summarizer.is_available() is False
    assert summarizer("text") is None
