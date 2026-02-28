# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Unit tests for pdf_analysis.service.embedding_store helpers."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from pdf_analysis.constants.metadata_keys import IND_SECTION_NUMBER_KEY
from pdf_analysis.service import embedding_store


def test_compute_document_hash_stable() -> None:
    h1 = embedding_store._compute_document_hash(
        s3_bucket="bucket",
        s3_key="key",
        version_id="v1",
        content="text",
    )
    h2 = embedding_store._compute_document_hash(
        s3_bucket="bucket",
        s3_key="key",
        version_id="v1",
        content="text",
    )
    assert h1 == h2


def test_vector_literal_format() -> None:
    literal = embedding_store._vector_literal([1, 2.5])
    assert literal == "[1.00000000,2.50000000]"


def test_build_embedding_text_includes_metadata(monkeypatch) -> None:
    store = embedding_store.SupabaseEmbeddingStore
    # Bypass __init__ to avoid env dependencies
    instance = store.__new__(store)
    instance.max_chars = 500
    text = instance._build_embedding_text(
        markdown="Body",
        metadata={IND_SECTION_NUMBER_KEY: "2.4"},
        filename="file.pdf",
        company="Acme",
        project="Rocket",
        module_label="Module 4",
    )
    assert "Document: file.pdf" in text
    assert "IND Section: 2.4" in text


def test_ensure_list() -> None:
    assert embedding_store._ensure_list("a,b") == ["a", "b"]
    assert embedding_store._ensure_list(["a", "b"]) == ["a", "b"]
    assert embedding_store._ensure_list(None) == []
