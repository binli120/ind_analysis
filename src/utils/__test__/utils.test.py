# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Unit tests for src/utils helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from utils import create_ind_folder, utils


def test_extract_event_time_uses_record_value() -> None:
    record = {"eventTime": "2025-01-01T00:00:00+00:00"}
    assert utils._extract_event_time(record) == record["eventTime"]


def test_extract_event_time_falls_back_to_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils, "_utc_now", lambda: "2025-01-02T03:04:05+00:00")
    assert utils._extract_event_time({}) == "2025-01-02T03:04:05+00:00"


def test_utc_now_returns_isoformat() -> None:
    timestamp = utils._utc_now()
    parsed = datetime.fromisoformat(timestamp)
    assert parsed.tzinfo is not None


def test_load_template_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        create_ind_folder.load_template(tmp_path / "missing.json")


def test_load_template_reads_json(tmp_path: Path) -> None:
    payload = {"2.4": {"overview": {}}}
    template = tmp_path / "template.json"
    template.write_text('{"2.4": {"overview": {}}}', encoding="utf-8")
    assert create_ind_folder.load_template(template) == payload


def test_create_structure_puts_expected_keys() -> None:
    created: List[Tuple[str, str]] = []

    class FakeS3:
        def put_object(self, *, Bucket: str, Key: str, **kwargs: Any) -> Dict[str, Any]:
            created.append((Bucket, Key))
            return {}

    structure = {"2.4": {}, "2.6": {"2.6.1": {}, "2.6.2": {}}}
    create_ind_folder.create_structure(FakeS3(), "bucket", "root/", structure)

    keys = {key for _, key in created}
    assert keys == {
        "root/2.4/",
        "root/2.6/",
        "root/2.6/2.6.1/",
        "root/2.6/2.6.2/",
    }
    assert {bucket for bucket, _ in created} == {"bucket"}
