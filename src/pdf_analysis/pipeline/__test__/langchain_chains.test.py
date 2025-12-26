# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Unit tests for pdf_analysis.pipeline.langchain_chains."""

from __future__ import annotations

import builtins

import pytest

from pdf_analysis.pipeline import langchain_chains


def test_pydantic_helper_returns_base_model() -> None:
    BaseModel, Field = langchain_chains._pydantic()
    assert BaseModel is not None
    assert Field is not None


def test_load_chat_model_missing_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):  # type: ignore[override]
        if name == "langchain_openai":
            raise ModuleNotFoundError("langchain_openai missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError):
        langchain_chains._load_chat_model()


def test_build_study_segmentation_chain_missing_langchain(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):  # type: ignore[override]
        if name == "langchain_core":
            raise ModuleNotFoundError("langchain_core missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises((RuntimeError, ImportError)):
        langchain_chains.build_study_segmentation_chain()
