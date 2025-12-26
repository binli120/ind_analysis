# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Unit tests for pdf_analysis.api.server helpers."""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import types
from typing import Any, Dict

import pandas as pd
import pytest


def _ensure_optional_stubs() -> None:
    try:
        import boto3  # noqa: F401
    except Exception:
        boto3_stub = types.ModuleType("boto3")

        def _client(*_args: Any, **_kwargs: Any) -> Any:
            return object()

        boto3_stub.client = _client  # type: ignore[attr-defined]
        sys.modules["boto3"] = boto3_stub

    try:
        from botocore.exceptions import ClientError  # noqa: F401
    except Exception:
        botocore_stub = types.ModuleType("botocore")
        exceptions_stub = types.ModuleType("botocore.exceptions")

        class ClientError(Exception):
            pass

        exceptions_stub.ClientError = ClientError  # type: ignore[attr-defined]
        sys.modules["botocore"] = botocore_stub
        sys.modules["botocore.exceptions"] = exceptions_stub


def _load_server_module() -> Any:
    _ensure_optional_stubs()
    module_path = Path(__file__).resolve().parents[1] / "server.py"
    spec = spec_from_file_location("pdf_analysis_api_server", module_path)
    assert spec is not None
    module = module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def server() -> Any:
    return _load_server_module()


def test_table_to_payload_limits_rows(server: Any) -> None:
    df = pd.DataFrame(
        [
            {"A": 1, "B": None},
            {"A": 2, "B": "text"},
        ]
    )
    table = {"page_number": 3, "index_on_page": 1, "engine": "pdfplumber", "dataframe": df}
    payload = server._table_to_payload(table, max_rows=1)

    assert payload["page_number"] == 3
    assert payload["row_count"] == 2
    assert payload["columns"] == ["A", "B"]
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["B"] == ""


def test_build_table_manifest_preview_rows(server: Any) -> None:
    df = pd.DataFrame([{"A": "x"}, {"A": "y"}])
    table = {"page_number": 1, "index_on_page": 1, "engine": "pdfplumber", "dataframe": df}
    manifest = server._build_table_manifest([table], preview_rows=1)

    assert len(manifest) == 1
    preview = manifest[0]["preview_rows"]
    assert list(preview["A"]) == ["x"]


def test_parse_columns_header(server: Any) -> None:
    assert server._parse_columns_header("A; B ; ; C") == ["A", "B", "C"]
    assert server._parse_columns_header(None) == []


def test_normalize_optional_uuid(server: Any) -> None:
    assert server._normalize_optional_uuid(None, "value") is None
    assert server._normalize_optional_uuid("", "value") is None
    with pytest.raises(Exception):
        server._normalize_optional_uuid("not-a-uuid", "value")
    valid = "6fa459ea-ee8a-3ca4-894e-db77e160355e"
    assert server._normalize_optional_uuid(valid, "value") == valid


def test_format_embedding_for_prompt(server: Any) -> None:
    assert server._format_embedding_for_prompt(None) == ""
    assert server._format_embedding_for_prompt([]) == ""
    result = server._format_embedding_for_prompt([0.1, 0.2])
    assert result.startswith("[") and result.endswith("]")
