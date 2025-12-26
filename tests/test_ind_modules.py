# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Tests covering IND pipeline modules and registry wiring."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import pytest

from ind_pipeline import MODULE_REGISTRY, STAGE_SEQUENCE, STAGE_SUCCESSORS
from ind_pipeline.pdf_extraction import pdf_extraction_handler
from ind_pipeline import pdf_extraction as pdf_extraction_module
from ind_pipeline import zeroshot_labeling as zeroshot_module
from ind_pipeline.zeroshot_labeling import zeroshot_handler


class DummyResult:
    """Lightweight stand-in for PipelineResult."""

    @dataclass
    class Metrics:
        total_pages: int = 1
        pages_with_text: int = 1
        text_coverage: float = 1.0
        ocr_pages: int = 0
        tables_total: int = 0
        table_pages: int = 0
        key_value_pairs: int = 0
        confidence: float = 1.0

    def __init__(self) -> None:
        self.text_engine = "pdfplumber"
        self.ocr_strategy = "none"
        self.table_engines = ("pdfplumber",)
        self.markdown = "# Document\n\nBody text."
        self.html = "<html><body>Body text.</body></html>"
        self.quality_report: Dict[str, Any] = {"status": "ok"}
        self.quality_markdown = "Quality details"
        self.key_values = {"foo": "bar"}
        self.metrics = self.Metrics()


def test_pdf_extraction_handler_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = {
        "company": "Acme",
        "project": "Rocket",
        "file": "proposal.pdf",
        "metadata": {"s3_uri": "s3://docs/proposal.pdf", "s3_version_id": "123"},
    }

    temp_pdf = tmp_path / "proposal.pdf"

    def fake_download(bucket: str, key: str, version_id: str | None) -> Path:
        temp_pdf.write_text("dummy pdf", encoding="utf-8")
        return temp_pdf

    stored_document: Dict[str, Any] = {}

    def fake_store(bucket: str, key: str, document: Dict[str, Any]) -> str:
        stored_document.update(document)
        return "analysis/docs/proposal.analysis.json"

    monkeypatch.setattr(pdf_extraction_module, "_download_pdf", fake_download)
    monkeypatch.setattr(pdf_extraction_module, "_run_pipeline", lambda _: DummyResult())
    monkeypatch.setattr(pdf_extraction_module, "_store_analysis", fake_store)

    result = pdf_extraction_handler(payload)

    assert result["analysis_s3_uri"].endswith("analysis/docs/proposal.analysis.json")
    assert stored_document["company"] == "Acme"
    assert stored_document["project"] == "Rocket"
    assert stored_document["source"]["bucket"] == "docs"
    assert not temp_pdf.exists()
    assert result["stage"] == "pdf-extraction"
    assert result["status"] == "completed"


def test_zeroshot_handler_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    analysis_doc = {
        "company": "Acme",
        "project": "Rocket",
        "file": "proposal.pdf",
        "markdown": "# Document",
        "source": {
            "bucket": "docs",
            "key": "proposal.pdf",
        },
    }

    def fake_load(uri: str):
        return analysis_doc, "analysis-bucket", "analysis/docs/proposal.analysis.json"

    stored_payload: Dict[str, Any] = {}

    def fake_store(bucket: str, key: str, document: Dict[str, Any]) -> str:
        stored_payload.update(document)
        return "metadata/docs/proposal.classification.json"

    class FakeGenerator:
        def __init__(self) -> None:
            self._client = object()

        def __call__(self, text: str) -> Dict[str, Any]:
            return {
                "labels": ["ind"],
                "keywords": ["rocket"],
                "language": "en",
                "ind_document_type": "summary",
                "ind_section_number": "2.3",
                "ind_section_title": "Quality Summary",
                "ind_classification_confidence": 0.9,
            }

    monkeypatch.setattr(zeroshot_module, "_load_analysis_document", fake_load)
    monkeypatch.setattr(zeroshot_module, "_store_metadata", fake_store)
    monkeypatch.setattr(zeroshot_module, "_get_generator", lambda: FakeGenerator())

    payload = {"analysis_s3_uri": "s3://analysis/docs/proposal.analysis.json"}
    result = zeroshot_handler(payload)

    assert result["metadata_s3_uri"].endswith("metadata/docs/proposal.classification.json")
    assert result["ind_section_number"] == "2.3"
    assert stored_payload["metadata"]["labels"] == ["ind"]
    assert result["stage"] == "zeroshot-labeling"
    assert result["status"] == "completed"


def test_module_registry_smoke() -> None:
    expected_modules = {
        "pdf-extraction",
        "zeroshot-labeling",
        "pdf-parsing-chunking",
        "metadata-summary-extraction",
    }
    assert expected_modules.issubset(MODULE_REGISTRY.keys())
    assert callable(MODULE_REGISTRY["pdf-extraction"].entrypoint)


def test_stage_sequence_consistency() -> None:
    assert STAGE_SEQUENCE[0] == "pdf-extraction"
    for stage, successors in STAGE_SUCCESSORS.items():
        assert stage in STAGE_SEQUENCE
        for successor in successors:
            assert successor in STAGE_SEQUENCE


def test_pdf_extraction_publishes_next_topic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    topic_arn = "arn:aws:sns:us-east-1:123456789012:zeroshot"
    monkeypatch.setenv("PDF_EXTRACT_NEXT_TOPIC_ARN", topic_arn)

    published: list[Dict[str, Any]] = []

    class FakeSNS:
        def publish(self, TopicArn: str, Message: str) -> None:
            published.append({"topic": TopicArn, "message": json.loads(Message)})

    monkeypatch.setattr(pdf_extraction_module, "_sns_client", FakeSNS())

    payload = {
        "company": "Acme",
        "project": "Rocket",
        "file": "proposal.pdf",
        "metadata": {"s3_uri": "s3://docs/proposal.pdf"},
    }

    temp_pdf = tmp_path / "proposal.pdf"

    def fake_download(bucket: str, key: str, version_id: str | None) -> Path:
        temp_pdf.write_text("dummy pdf", encoding="utf-8")
        return temp_pdf

    def fake_store(bucket: str, key: str, document: Dict[str, Any]) -> str:
        return "analysis/docs/proposal.analysis.json"

    monkeypatch.setattr(pdf_extraction_module, "_download_pdf", fake_download)
    monkeypatch.setattr(pdf_extraction_module, "_run_pipeline", lambda _: DummyResult())
    monkeypatch.setattr(pdf_extraction_module, "_store_analysis", fake_store)

    pdf_extraction_handler(payload)

    assert published
    assert published[0]["topic"] == topic_arn
    assert published[0]["message"]["stage"] == "pdf-extraction"
    assert published[0]["message"]["input"]["file"] == "proposal.pdf"


def test_zeroshot_publishes_next_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    topic_arn = "arn:aws:sns:us-east-1:123456789012:metadata-summary"
    monkeypatch.setenv("ZEROSHOT_NEXT_TOPIC_ARN", topic_arn)

    published: list[Dict[str, Any]] = []

    class FakeSNS:
        def publish(self, TopicArn: str, Message: str) -> None:
            published.append({"topic": TopicArn, "message": json.loads(Message)})

    monkeypatch.setattr(zeroshot_module, "_sns_client", FakeSNS())

    analysis_doc = {
        "company": "Acme",
        "project": "Rocket",
        "file": "proposal.pdf",
        "markdown": "# Document",
        "source": {
            "bucket": "docs",
            "key": "proposal.pdf",
        },
    }

    def fake_load_analysis(_: str) -> tuple[Dict[str, Any], str, str]:
        return (analysis_doc, "analysis-bucket", "analysis/docs/proposal.analysis.json")

    def fake_store_metadata(bucket: str, key: str, doc: Dict[str, Any]) -> str:
        _ = (bucket, key, doc)
        return "metadata/docs/proposal.classification.json"

    monkeypatch.setattr(zeroshot_module, "_load_analysis_document", fake_load_analysis)
    monkeypatch.setattr(zeroshot_module, "_store_metadata", fake_store_metadata)

    class FakeGenerator:
        def __init__(self) -> None:
            self._client = object()

        def __call__(self, text: str) -> Dict[str, Any]:
            return {
                "labels": ["ind"],
                "keywords": ["rocket"],
                "language": "en",
                "ind_document_type": "summary",
                "ind_section_number": "2.3",
                "ind_section_title": "Quality Summary",
                "ind_classification_confidence": 0.9,
            }

    monkeypatch.setattr(zeroshot_module, "_get_generator", lambda: FakeGenerator())

    zeroshot_handler({"analysis_s3_uri": "s3://analysis/docs/proposal.analysis.json"})

    assert published
    assert published[0]["topic"] == topic_arn
    assert published[0]["message"]["stage"] == "zeroshot-labeling"
    assert published[0]["message"]["output"]["labels"] == ["ind"]
