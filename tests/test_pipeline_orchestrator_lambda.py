# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Tests for the pipeline orchestrator Lambda event handling."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture(name="app_module")
def _app_module(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PIPELINE_STATUS_TABLE", "pipeline-status")
    monkeypatch.setenv("INGEST_COMPLETED_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:ingest-completed")
    monkeypatch.setenv("DEFAULT_COMPANY", "Acme")
    monkeypatch.setenv("DEFAULT_PROJECT", "Rocket")

    module = importlib.import_module("lambda.pipeline_orchestrator.app")
    importlib.reload(module)
    return module


def test_s3_event_records_document_and_publishes_event(monkeypatch: pytest.MonkeyPatch, app_module) -> None:
    published: List[Dict[str, Any]] = []

    class FakeTable:
        def __init__(self) -> None:
            self.items: List[Dict[str, Any]] = []

        def put_item(self, Item: Dict[str, Any]) -> None:
            self.items.append(Item)

        def update_item(self, *args, **kwargs) -> None:  # pragma: no cover - not used in this test
            raise AssertionError("update_item should not be called for S3 flow")

    table = FakeTable()
    monkeypatch.setattr(app_module, "_get_table", lambda: table)

    def fake_publish(**kwargs):
        kwargs["Message"] = json.loads(kwargs["Message"])
        published.append(kwargs)

    monkeypatch.setattr(app_module, "sns", SimpleNamespace(publish=fake_publish))

    event = {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventTime": "2024-10-01T12:00:00Z",
                "s3": {
                    "bucket": {"name": "doc-bucket"},
                    "object": {"key": "submissions/file.pdf", "versionId": "1"},
                },
            }
        ]
    }

    response = app_module.handler(event, None)

    assert response["status"] == "ok"
    assert table.items
    item = table.items[0]
    assert item["document_id"] == "doc-bucket/submissions/file.pdf#1"
    assert published
    assert published[0]["TopicArn"].endswith("ingest-completed")
    assert published[0]["Message"]["metadata"]["document_id"] == "doc-bucket/submissions/file.pdf#1"


def test_sns_event_updates_stage_status(monkeypatch: pytest.MonkeyPatch, app_module) -> None:
    updates: List[Dict[str, Any]] = []

    class FakeTable:
        def update_item(self, **kwargs) -> None:
            updates.append(kwargs)

        def put_item(self, Item: Dict[str, Any]) -> None:  # pragma: no cover - not used in this test
            raise AssertionError("put_item should not be called for SNS flow")

    monkeypatch.setattr(app_module, "_get_table", lambda: FakeTable())

    message = {
        "stage": "pdf-extraction",
        "status": "completed",
        "input": {"metadata": {"document_id": "doc-bucket/submissions/file.pdf#1"}},
        "output": {"analysis_s3_uri": "s3://doc-bucket/analysis/file.pdf"},
    }

    event = {
        "Records": [
            {
                "eventSource": "aws:sns",
                "Sns": {"Message": json.dumps(message)},
            }
        ]
    }

    response = app_module.handler(event, None)

    assert response["status"] == "ok"
    assert updates
    update_call = updates[0]
    assert update_call["Key"]["document_id"] == "doc-bucket/submissions/file.pdf#1"
    assert update_call["ExpressionAttributeValues"][":stage_status"] == "completed"
