# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Tests for CTD template loading behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ncd.config import ctd_template


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_load_template_entries_reads_single_configured_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    template = tmp_path / "template.json"
    module1_only = {"Section": "1.1", "Subsection": "1.1"}
    _write_json(template, {"Module 1 (Auto Template)": [module1_only]})
    monkeypatch.setattr(ctd_template, "_template_paths", lambda: [template])

    entries = ctd_template.load_template_entries()

    assert len(entries) == 1
    assert entries[0]["Section"] == "1.1"


def test_load_template_entries_returns_empty_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(ctd_template, "_template_paths", lambda: [missing])

    entries = ctd_template.load_template_entries()

    assert entries == []
