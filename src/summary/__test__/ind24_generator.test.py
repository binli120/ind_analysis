# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Unit tests for summary/ind24_generator.py pure helpers."""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from typing import Any

import pytest


def _load_ind24_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "ind24_generator.py"
    spec = spec_from_file_location("summary_ind24_generator", module_path)
    assert spec is not None
    module = module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ind24() -> Any:
    return _load_ind24_module()


def test_slugify_normalizes_values(ind24: Any) -> None:
    assert ind24._slugify("Project Name 2025") == "project-name-2025"
    assert ind24._slugify("###") == "unknown"


def test_truncate_text_noop(ind24: Any) -> None:
    text = "short text"
    assert ind24._truncate_text(text, 0, "Label") == text
    assert ind24._truncate_text(text, 50, "Label") == text


def test_truncate_text_truncates(ind24: Any) -> None:
    text = "abcdef"
    output = ind24._truncate_text(text, 3, "Label")
    assert output.startswith("[TRUNCATED")
    assert output.endswith("abc")


def test_chunk_markdown_splits(ind24: Any) -> None:
    markdown = "# Intro\nLine one\n\n## Details\nLine two\nLine three\n"
    chunks = ind24._chunk_markdown(markdown, token_limit=5, max_chunks=5)
    assert len(chunks) >= 2
    titles = {chunk.get("title") for chunk in chunks}
    assert "Intro" in titles or "Details" in titles


def test_merge_gap_structured_merges_severity(ind24: Any) -> None:
    payloads = [
        {
            "missing_sections": [
                {
                    "section_number": "2.6.1",
                    "section_name": "Intro",
                    "severity": "MINOR",
                    "description": "desc a",
                }
            ]
        },
        {
            "missing_sections": [
                {
                    "section_number": "2.6.1",
                    "section_name": "Intro",
                    "severity": "CRITICAL",
                    "description": "desc b",
                }
            ],
            "incomplete_sections": [
                {
                    "section_number": "2.6.2",
                    "missing_elements": ["elem"],
                    "recommendation": "do work",
                }
            ],
        },
    ]
    merged = ind24._merge_gap_structured(payloads)
    missing = merged.get("missing_sections") or []
    assert len(missing) == 1
    assert missing[0].get("severity") == "CRITICAL"
    description = missing[0].get("description") or ""
    assert "desc a" in description
    assert "desc b" in description


def test_format_gap_markdown_includes_sections(ind24: Any) -> None:
    gap_result = {
        "model": "test-model",
        "structured": {
            "missing_sections": [
                {
                    "section_number": "2.6.1",
                    "section_name": "Intro",
                    "severity": "MAJOR",
                    "description": "desc",
                    "recommended_action": "fix",
                }
            ],
            "incomplete_sections": [],
            "coverage_notes": "coverage",
        },
        "validation": {"confidence": 0.5, "issues": ["issue"]},
        "auto_generated_sections": [
            {
                "section_number": "2.6.2",
                "section_name": "Details",
                "generated_text": "draft",
            }
        ],
    }
    output = ind24._format_gap_markdown(gap_result)
    assert "# Section 2.6 Gap Analysis" in output
    assert "## Missing Sections" in output
    assert "2.6.1" in output
    assert "## Auto-Generated Drafts" in output


def test_format_summary_markdown_includes_metadata(ind24: Any) -> None:
    summary_result = {
        "model": "test-model",
        "summary": {
            "document_metadata": {"sponsor": "Acme"},
            "gap_analysis": {
                "missing_sections": [
                    {
                        "section_number": "2.4.2",
                        "section_name": "Pharmacology",
                        "severity": "MAJOR",
                        "description": "missing",
                    }
                ],
                "incomplete_sections": [],
            },
            "section_2_4_content": {
                "2.4.1_introduction": "Intro text",
                "2.4.2_pharmacology_summary": {"key": "value"},
            },
            "summary_statistics": {"total_sections": 2},
        },
        "validation": {"confidence": 0.9, "issues": []},
    }
    output = ind24._format_summary_markdown(summary_result)
    assert "## Document Metadata" in output
    assert "## Section 2.4 Content" in output
    assert "2.4.1_introduction" in output
    assert "Summary Statistics" in output


def test_validate_gap_payload_scores(ind24: Any) -> None:
    payload = {
        "missing_sections": [
            {
                "section_number": "2.6.1",
                "section_name": "Intro",
                "severity": "MINOR",
                "description": "desc",
            }
        ],
        "incomplete_sections": [
            {
                "section_number": "2.6.2",
                "missing_elements": ["elem"],
                "recommendation": "fix",
            }
        ],
    }
    result = ind24.validate_gap_payload(payload, chunk_count=2)
    assert "confidence" in result
    assert result.get("issues")


def test_validate_summary_payload_flags_missing_sections(ind24: Any) -> None:
    summary_payload = {
        "summary": {
            "section_2_4_content": {
                "2.4.1_introduction": "Intro text",
            }
        }
    }
    result = ind24.validate_summary_payload(summary_payload)
    issues = result.get("issues") or []
    assert any("Missing sections" in issue for issue in issues)
