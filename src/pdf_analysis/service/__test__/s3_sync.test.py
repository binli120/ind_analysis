# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Unit tests for pdf_analysis.service.s3_sync helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from pdf_analysis.service import s3_sync


def test_slugify() -> None:
    assert s3_sync._slugify("Module 4") == "module-4"


def test_safe_version() -> None:
    assert s3_sync._safe_version("v1:2") == "v1-2"
    assert s3_sync._safe_version(None) is None


def test_stringify() -> None:
    assert s3_sync._stringify({"a": 1}) == '{"a": 1}'
    assert s3_sync._stringify(None) == ""


def test_extract_document_metadata() -> None:
    service = s3_sync.S3RedisSyncService(s3_sync.S3SyncConfig(bucket="b"))
    parsed = service._extract_document_metadata("filynai.com/Proj/Module 4/file.pdf")
    assert parsed.company == "filynai.com"
    assert parsed.project == "Proj"
    assert parsed.module_number == 4


def test_extract_document_metadata_invalid() -> None:
    service = s3_sync.S3RedisSyncService(s3_sync.S3SyncConfig(bucket="b"))
    with pytest.raises(ValueError):
        service._extract_document_metadata("invalid/key.pdf")


def test_is_not_found_error() -> None:
    class FakeError(Exception):
        def __init__(self):
            self.response = {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}}

    assert s3_sync._is_not_found_error(FileNotFoundError("missing")) is True
    assert s3_sync._is_not_found_error(FakeError()) is True


def test_persist_markdown_file(tmp_path: Path) -> None:
    service = s3_sync.S3RedisSyncService(s3_sync.S3SyncConfig(bucket="b"))
    doc = s3_sync.S3Document(
        key="filynai.com/Proj/Module 4/file.pdf",
        company="filynai.com",
        project="Proj",
        module_label="Module 4",
        module_number=4,
        version_id="v1",
        last_modified=datetime.now(timezone.utc),
    )
    service._persist_markdown_file(tmp_path, doc, "text", {"meta": True}, summary_text="summary")
    assert (tmp_path / "filynai.com/Proj/Module 4/file.v1.pdf.md").exists()
    assert (tmp_path / "filynai.com/Proj/Module 4/file.v1.pdf.meta.json").exists()
    assert (tmp_path / "filynai.com/Proj/Module 4/file.v1.pdf.summary.txt").exists()
