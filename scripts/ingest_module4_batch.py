#!/usr/bin/env python
# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""
Batch runner to ingest Module 4 PDFs from S3 into the NCD database.

This script can orchestrate multiple pipelines per PDF:
  - Core ingestion (S3 -> markdown/quality + documents/document_versions + ingestion status)
  - NCD tox extraction
  - NCD PK extraction

Routing logic (auto mode):
  - If markdown sidecar is missing (or ingestion status incomplete), run core.
  - If tox data is missing in DB, run tox extraction.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set
from urllib.parse import unquote_plus
from uuid import uuid4

try:
    import boto3
except ModuleNotFoundError:  # pragma: no cover - optional in CI unit tests
    boto3 = None  # type: ignore[assignment]

try:
    from botocore.exceptions import ClientError
except ModuleNotFoundError:  # pragma: no cover - optional in CI unit tests
    class ClientError(Exception):
        pass
from sqlalchemy import text as sqltext

# Allow running from repo root without installing the package.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from ncd.types.ctd_elements import build_ctd_element_reference, list_template_elements
from ncd.types.ctd_materials import module4_sections_for_ctd
from database.db_interface import CTDSectionReferenceRecord, NCDRepository
from ncd.llm.llm_client import LLMClient
from ncd.pipeline.pipeline_runner import run_pdf_ingest_and_extract
from ncd.ingestion.pdf_ingestion import sha256_file
from ind_pipeline import metadata_summary_extraction as section_summary
from ncd.ingestion.section_detector import SectionSpan, persist_section_spans
from pdf_analysis.sqs_worker import process_message


_MODULE_PATTERN = re.compile(r"^module\s*(?P<number>\d+)(?:[\s._-].*)?$", re.IGNORECASE)
_SECTION_TOKEN_PATTERN = re.compile(r"\d+(?:\.\d+)+(?:\|\d+(?:\.\d+)+)*")
_TRY_AGAIN_IN_SECONDS_PATTERN = re.compile(
    r"try again in\s+([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE
)
_PHARM_OVERVIEW_SECTION = "2.6.3.1"
_MODULE4_PHARM_SECTION_RE = re.compile(r"(?<!\d)(4\.2\.1\.[1-4])(?!\d)")
_MARKDOWN_PAGE_RE = re.compile(r"^## Page\s+(\d+)\b", re.MULTILINE)
_MARKDOWN_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_INPUT_TABLE_STUDY_RE = re.compile(r"^(?:input\.p\d+\.t\d+|p\d+\.t\d+)$", re.IGNORECASE)
_PHARM_STUDY_TYPE_BY_SECTION = {
    "4.2.1.1": "Primary pharmacodynamics",
    "4.2.1.2": "Secondary pharmacodynamics",
    "4.2.1.3": "Safety pharmacology",
    "4.2.1.4": "Pharmacodynamic drug interactions",
}
_ROUTE_PATTERNS = (
    "intravenous",
    "intraperitoneal",
    "intramuscular",
    "subcutaneous",
    "intradermal",
    "oral",
    "topical",
    "intranasal",
    "inhalation",
    "intrathecal",
    "intravitreal",
)
_SPECIES_PATTERNS = (
    "cynomolgus monkey",
    "rhesus monkey",
    "beagle dog",
    "sprague-dawley rat",
    "wistar rat",
    "c57bl/6 mice",
    "c57 bl/6 mice",
    "mouse",
    "mice",
    "rat",
    "rabbit",
    "dog",
    "monkey",
)
_FACILITY_HINTS = (
    "inc",
    "llc",
    "ltd",
    "corp",
    "corporation",
    "company",
    "co.",
    "co,",
    "laborator",
    "labs",
    "lab ",
    "institute",
    "university",
    "center",
    "centre",
    "biotech",
    "pharma",
    "contract research",
    "cro",
    "facility",
)
_FACILITY_EXCLUDE_PATTERNS = (
    re.compile(r"(?i)\bmg\s*/?\s*kg\b"),
    re.compile(r"(?i)\bdos(?:e|ing)\b"),
    re.compile(r"(?i)\b(?:day|week|month)s?\b"),
    re.compile(r"(?i)\b(?:rats?|mice|mouse|monkeys?|dogs?|rabbits?)\b"),
)
_FACILITY_SENTENCE_PATTERNS = (
    re.compile(
        r"(?im)\b(?:testing|test)\s+facilit(?:y|ies)\b(?:\s*\([^)]*\))?\s*(?:was|were|is|are|:|-)\s*([^\n;]+)"
    ),
    re.compile(
        r"(?im)\b(?:study|studies|work|experiments?)\b[^.\n]{0,80}?\b(?:conducted|performed|carried out)\s+(?:at|by)\s+([^\n;]+)"
    ),
    re.compile(
        r"(?im)\b(?:contract\s+research\s+organization|cro)\b(?:\s*\([^)]*\))?\s*(?::|-|was|is)?\s*([^\n;]+)"
    ),
)
_PHARM_OVERVIEW_MODULE4_SECTIONS: Optional[Set[str]] = None


def _require_boto3() -> Any:
    if boto3 is None:
        raise RuntimeError(
            "boto3 is required for S3 operations in ingest_module4_batch.py."
        )
    return boto3


def _sanitize_section_suffix(name: str) -> str:
    cleaned = re.sub(r"\s+", "_", name.strip())
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "", cleaned)
    return cleaned or "document"


def _clean_section_title(segment: str, token: str) -> Optional[str]:
    title = segment.replace(token, "")
    title = title.replace("|", " ")
    title = title.replace('"', "").replace("'", "")
    title = re.sub(r"^[\s._-]+", "", title)
    title = re.sub(r"\s{2,}", " ", title).strip()
    return title or None


def _derive_section_from_key(key: str) -> tuple[str, Optional[str]]:
    parts = key.split("/")
    file_stem = Path(key).stem
    suffix = _sanitize_section_suffix(file_stem)
    section_token = None
    section_title = None
    for segment in parts[:-1]:
        match = _SECTION_TOKEN_PATTERN.search(segment)
        if match:
            section_token = match.group(0)
            section_title = _clean_section_title(segment, section_token)
    if section_token:
        section_number = f"{section_token}.{suffix}"
    else:
        section_number = suffix
    if not section_title:
        section_title = file_stem
    return section_number, section_title


def _pharm_overview_module4_sections() -> Set[str]:
    global _PHARM_OVERVIEW_MODULE4_SECTIONS
    if _PHARM_OVERVIEW_MODULE4_SECTIONS is not None:
        return _PHARM_OVERVIEW_MODULE4_SECTIONS

    sections: Set[str] = set()
    try:
        for entry in module4_sections_for_ctd(_PHARM_OVERVIEW_SECTION):
            if not isinstance(entry, dict):
                continue
            section = str(entry.get("module4_section") or "").strip()
            if section:
                sections.add(section)
    except Exception:
        sections = set()

    if not sections:
        sections = {"4.2.1.1", "4.2.1.2", "4.2.1.3", "4.2.1.4"}

    _PHARM_OVERVIEW_MODULE4_SECTIONS = sections
    return sections


def _extract_module4_section_from_key(
    key: str,
    *,
    allowed_sections: Optional[Set[str]] = None,
) -> Optional[str]:
    allowed = allowed_sections or _pharm_overview_module4_sections()
    for match in _MODULE4_PHARM_SECTION_RE.findall(key or ""):
        if match in allowed:
            return match
    return None


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _remove_markdown_tables(text: str) -> str:
    return "\n".join(
        line for line in str(text or "").splitlines() if not _MARKDOWN_TABLE_LINE_RE.match(line)
    )


def _extract_pages_text(markdown: str, *, max_pages: int = 3) -> str:
    raw = str(markdown or "")
    matches = list(_MARKDOWN_PAGE_RE.finditer(raw))
    if not matches:
        return raw[:30000]

    parts: List[str] = []
    for idx, match in enumerate(matches):
        try:
            page_no = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if page_no > max_pages:
            continue
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        chunk = raw[start:end].strip()
        if chunk:
            parts.append(chunk)
    if not parts:
        return raw[:30000]
    return "\n\n".join(parts)


def _extract_label_values(text: str, labels: Sequence[str]) -> List[str]:
    matches: List[str] = []
    haystack = str(text or "")
    for label in labels:
        escaped = re.escape(label)
        line_pattern = re.compile(
            rf"(?im)^\s*{escaped}\s*(?:[:\-]\s*|\|\s*|\s{{2,}})([^|\n\r]+)"
        )
        for match in line_pattern.finditer(haystack):
            value = _normalize_space(match.group(1))
            if value:
                matches.append(value)
    return matches


def _normalize_study_number(value: str) -> str:
    candidate = _normalize_space(value).strip(" .,:;")
    if not candidate:
        return ""
    candidate = re.sub(
        r"(?i)^(?:study|report|protocol)\s*(?:number|no\.?|id|#)\s*[:\-]?\s*",
        "",
        candidate,
    ).strip(" .,:;")
    candidate = re.split(r"[;,\t]|(?:\s{2,})", candidate, maxsplit=1)[0].strip()
    if not candidate:
        return ""
    if _INPUT_TABLE_STUDY_RE.fullmatch(candidate):
        return ""
    if candidate.lower().startswith("u201") or candidate.lower().startswith("u00"):
        return ""
    if not re.search(r"\d", candidate):
        return ""
    if re.search(r"[A-Za-z]", candidate):
        return candidate
    # Accept legacy numeric sponsor/report IDs (for example, 301644 or 1006-2525).
    if re.fullmatch(r"\d{5,}", candidate):
        return candidate
    if re.fullmatch(r"\d{2,}(?:[-./]\d{2,})+", candidate):
        return candidate
    # Reject short or weak numeric fragments such as page/table artifacts.
    if re.fullmatch(r"\d{1,4}", candidate):
        return ""
    return ""


def _extract_study_number(text: str, file_name: str, content_hash: str) -> str:
    label_values = _extract_label_values(
        text,
        (
            "Study Number",
            "Study No.",
            "Study No",
            "Study ID",
            "Report Number",
            "Report No.",
            "Protocol Number",
            "Protocol No.",
        ),
    )
    for value in label_values:
        study_number = _normalize_study_number(value)
        if study_number:
            return study_number

    stem = Path(file_name).stem
    fallback_tokens = re.findall(
        r"(?i)\b(?=[A-Z0-9.-]*[A-Z])(?=[A-Z0-9.-]*\d)[A-Z0-9]{2,}(?:[-.][A-Z0-9]+)+\b",
        stem,
    )
    for token in fallback_tokens:
        study_number = _normalize_study_number(token)
        if study_number:
            return study_number

    digest = (content_hash or "")[:8].upper()
    if not digest:
        digest = "00000000"
    digits = "".join(ch for ch in digest if ch.isdigit())
    if len(digits) < 3:
        digits = (digits + "000")[:3]
    else:
        digits = digits[:3]
    return f"DOC-{digits}-{digest}"


def _extract_study_identifiers(
    text: str,
    file_name: str,
    content_hash: str,
) -> Dict[str, str]:
    sponsor = ""
    cro = ""
    sponsor_match = re.search(
        r"(?im)sponsor[^\n:;]*study(?:\s*(?:number|no\.?|id|#))?\s*[:#-]\s*([^;|\n]+)",
        str(text or ""),
    )
    if sponsor_match:
        sponsor = _normalize_study_number(sponsor_match.group(1))
    cro_match = re.search(
        r"(?im)cro[^\n:;]*study(?:\s*(?:number|no\.?|id|#))?\s*[:#-]\s*([^;|\n]+)",
        str(text or ""),
    )
    if cro_match:
        cro = _normalize_study_number(cro_match.group(1))
    primary = sponsor or _extract_study_number(text, file_name, content_hash)
    if not sponsor:
        sponsor = primary
    display_parts: List[str] = []
    if sponsor:
        display_parts.append(f"Sponsor study #: {sponsor}")
    if cro and cro.lower() != sponsor.lower():
        display_parts.append(f"CRO study #: {cro}")
    display = "; ".join(display_parts) if display_parts else primary
    return {
        "study_number": primary,
        "study_number_display": display,
        "sponsor_study_number": sponsor,
        "cro_study_number": cro,
    }


def _collect_study_number_aliases(*values: str) -> List[str]:
    aliases: List[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        aliases.append(normalized)
    return aliases


def _extract_glp_statement(text: str) -> str:
    for line in str(text or "").splitlines():
        normalized = _normalize_space(line)
        if not normalized:
            continue
        if re.search(r"(?i)\bgood laboratory practice\b|\bnon[- ]?glp\b|\bglp\b", normalized):
            return normalized[:300]
    return ""


def _extract_glp_status(text: str) -> str:
    lowered = str(text or "").lower()
    if re.search(r"\bnon[- ]?glp\b|\bnot\b[^\n]{0,60}\bglp\b", lowered):
        return "Non-GLP"
    if re.search(r"\bgood laboratory practice\b|\bglp\b", lowered):
        return "GLP"
    return ""


def _extract_primary_species(text: str) -> str:
    lowered = str(text or "").lower()
    for pattern in _SPECIES_PATTERNS:
        if pattern in lowered:
            return pattern.title()
    return ""


def _extract_route(text: str) -> str:
    lowered = str(text or "").lower()
    for pattern in _ROUTE_PATTERNS:
        if pattern in lowered:
            return pattern
    return ""


def _extract_testing_facility(text: str) -> str:
    def _clean_candidate(value: str) -> str:
        cleaned = _normalize_space(value).strip(" ,:;()[]{}\"'")
        cleaned = re.sub(
            r"(?i)^(?:at|by|the|testing facility|test facility|facility|laboratory)\s+",
            "",
            cleaned,
        ).strip(" ,:;")
        return cleaned

    def _looks_plausible(value: str) -> bool:
        lowered = value.lower()
        if not value or len(value) < 3:
            return False
        if lowered in {"na", "n/a", "none", "unknown", "not applicable"}:
            return False
        if any(pattern.search(value) for pattern in _FACILITY_EXCLUDE_PATTERNS):
            return False
        if not re.search(r"[A-Za-z]", value):
            return False
        if len(value.split()) > 20:
            return False
        return True

    candidates: List[str] = []
    seen: set[str] = set()

    def _append_candidate(raw: str) -> None:
        candidate = _clean_candidate(raw)
        if not _looks_plausible(candidate):
            return
        key = candidate.lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    values = _extract_label_values(
        text,
        (
            "Testing Facility",
            "Test Facility",
            "Facility",
            "Testing Laboratory",
            "Laboratory",
            "Performed At",
            "Conducted At",
            "Sponsor",
        ),
    )
    for value in values:
        _append_candidate(value)

    for pattern in _FACILITY_SENTENCE_PATTERNS:
        for match in pattern.finditer(str(text or "")):
            _append_candidate(match.group(1))

    for value in candidates:
        lowered = value.lower()
        if any(token in lowered for token in _FACILITY_HINTS):
            return value
    return candidates[0] if candidates else ""


def _extract_test_system(text: str) -> str:
    values = _extract_label_values(
        text,
        (
            "Test System",
            "Species/Strain",
            "Species / Strain",
            "Species",
            "Strain",
            "Animal Model",
        ),
    )
    for value in values:
        if value and len(value) <= 240:
            return value
    species = _extract_primary_species(text)
    return species


def _extract_method_of_administration(text: str) -> str:
    values = _extract_label_values(
        text,
        (
            "Method of Administration",
            "Route of Administration",
            "Administration Route",
            "Route",
            "Dosing Route",
        ),
    )
    for value in values:
        if value and len(value) <= 240:
            return value

    route = _extract_route(text)
    if route:
        return route
    return ""


def _module4_section_title(module4_section: str) -> str:
    return _PHARM_STUDY_TYPE_BY_SECTION.get(module4_section, "Pharmacology study")


def _extract_pharm_overview_metadata(
    *,
    key: str,
    file_name: str,
    module4_section: str,
    markdown: str,
    content_hash: str,
) -> Dict[str, str]:
    first_pages = _extract_pages_text(markdown, max_pages=3)
    text = _remove_markdown_tables(first_pages)
    identifiers = _extract_study_identifiers(text, file_name, content_hash)
    test_system = _extract_test_system(text)
    method = _extract_method_of_administration(text)
    facility = _extract_testing_facility(text)
    glp_statement = _extract_glp_statement(text)
    glp_status = _extract_glp_status(text)
    study_title = Path(file_name).stem.replace("_", " ").strip()
    type_of_study = _module4_section_title(module4_section)
    location_in_ctd = f'Module 4, Section {module4_section}: "{study_title}"'

    return {
        "study_number": identifiers.get("study_number", ""),
        "study_number_display": identifiers.get("study_number_display", ""),
        "sponsor_study_number": identifiers.get("sponsor_study_number", ""),
        "cro_study_number": identifiers.get("cro_study_number", ""),
        "test_system": test_system,
        "method_of_administration": method,
        "testing_facility": facility,
        "glp_status": glp_status,
        "glp_compliance": glp_statement or glp_status,
        "type_of_study": type_of_study,
        "study_title": study_title,
        "location_in_ctd": location_in_ctd,
    }


def _upsert_pharm_overview_to_db(
    *,
    repo: NCDRepository,
    project_id: str,
    file_name: str,
    content_hash: str,
    module4_section: str,
    metadata: Dict[str, str],
) -> tuple[str, str]:
    source_document_id = repo.upsert_source_document(
        project_id=project_id,
        file_name=file_name,
        module="Module 4",
        ctd_section=module4_section,
        sha256=content_hash,
    )

    study_number = str(metadata.get("study_number") or "").strip()
    sponsor_study_number = str(metadata.get("sponsor_study_number") or "").strip()
    cro_study_number = str(metadata.get("cro_study_number") or "").strip()
    study_number_aliases = _collect_study_number_aliases(
        study_number,
        sponsor_study_number,
        cro_study_number,
    )
    if not study_number and study_number_aliases:
        study_number = study_number_aliases[0]
    primary_study_number = sponsor_study_number or study_number
    if not primary_study_number and study_number_aliases:
        primary_study_number = study_number_aliases[0]
    alias_1 = study_number_aliases[0] if len(study_number_aliases) > 0 else ""
    alias_2 = study_number_aliases[1] if len(study_number_aliases) > 1 else ""
    alias_3 = study_number_aliases[2] if len(study_number_aliases) > 2 else ""
    test_system = str(metadata.get("test_system") or "").strip()
    method = str(metadata.get("method_of_administration") or "").strip()
    glp_status = str(metadata.get("glp_status") or "").strip()
    species = _extract_primary_species(test_system)
    route = _extract_route(method)
    if not route:
        route = _extract_route(str(metadata.get("location_in_ctd") or ""))
    study_type = (
        str(metadata.get("type_of_study") or "").strip().lower().replace(" ", "_")
        or "pharmacology"
    )
    extra_json = json.dumps(
        {
            "type_of_study": str(metadata.get("type_of_study") or "").strip(),
            "test_system": test_system,
            "method_of_administration": method,
            "testing_facility": str(metadata.get("testing_facility") or "").strip(),
            "glp_compliance": str(metadata.get("glp_compliance") or "").strip(),
            "study_title": str(metadata.get("study_title") or "").strip(),
            "study_number_display": str(
                metadata.get("study_number_display") or ""
            ).strip(),
            "sponsor_study_number": sponsor_study_number,
            "cro_study_number": cro_study_number,
            "study_number_aliases": study_number_aliases,
            "location_in_ctd": str(metadata.get("location_in_ctd") or "").strip(),
        }
    )

    db = repo.session
    existing = (
        db.execute(
            sqltext(
                """
                SELECT id
                FROM ncd_study
                WHERE project_id = :pid
                  AND (
                    main_source_document_id = :source_document_id
                    OR (
                        :alias_1 <> ''
                        AND (
                            LOWER(COALESCE(sponsor_study_id, '')) = LOWER(:alias_1)
                            OR LOWER(COALESCE(extra_attributes ->> 'sponsor_study_number', '')) = LOWER(:alias_1)
                            OR LOWER(COALESCE(extra_attributes ->> 'cro_study_number', '')) = LOWER(:alias_1)
                            OR EXISTS (
                                SELECT 1
                                FROM jsonb_array_elements_text(
                                    COALESCE(extra_attributes -> 'study_number_aliases', '[]'::jsonb)
                                ) AS existing_alias(value)
                                WHERE LOWER(existing_alias.value) = LOWER(:alias_1)
                            )
                        )
                    )
                    OR (
                        :alias_2 <> ''
                        AND (
                            LOWER(COALESCE(sponsor_study_id, '')) = LOWER(:alias_2)
                            OR LOWER(COALESCE(extra_attributes ->> 'sponsor_study_number', '')) = LOWER(:alias_2)
                            OR LOWER(COALESCE(extra_attributes ->> 'cro_study_number', '')) = LOWER(:alias_2)
                            OR EXISTS (
                                SELECT 1
                                FROM jsonb_array_elements_text(
                                    COALESCE(extra_attributes -> 'study_number_aliases', '[]'::jsonb)
                                ) AS existing_alias(value)
                                WHERE LOWER(existing_alias.value) = LOWER(:alias_2)
                            )
                        )
                    )
                    OR (
                        :alias_3 <> ''
                        AND (
                            LOWER(COALESCE(sponsor_study_id, '')) = LOWER(:alias_3)
                            OR LOWER(COALESCE(extra_attributes ->> 'sponsor_study_number', '')) = LOWER(:alias_3)
                            OR LOWER(COALESCE(extra_attributes ->> 'cro_study_number', '')) = LOWER(:alias_3)
                            OR EXISTS (
                                SELECT 1
                                FROM jsonb_array_elements_text(
                                    COALESCE(extra_attributes -> 'study_number_aliases', '[]'::jsonb)
                                ) AS existing_alias(value)
                                WHERE LOWER(existing_alias.value) = LOWER(:alias_3)
                            )
                        )
                    )
                  )
                ORDER BY
                  CASE WHEN main_source_document_id = :source_document_id THEN 0 ELSE 1 END,
                  updated_at DESC NULLS LAST,
                  created_at DESC
                LIMIT 1
                """
            ),
            {
                "pid": project_id,
                "source_document_id": source_document_id,
                "study_number": study_number,
                "alias_1": alias_1,
                "alias_2": alias_2,
                "alias_3": alias_3,
            },
        )
        .mappings()
        .first()
    )

    if existing:
        study_id = str(existing.get("id"))
        db.execute(
            sqltext(
                """
                UPDATE ncd_study
                SET project_id = COALESCE(project_id, :pid),
                    sponsor_study_id = COALESCE(NULLIF(sponsor_study_id, ''), NULLIF(:primary_study_number, ''), :study_number),
                    study_type = COALESCE(NULLIF(study_type, ''), :study_type),
                    glp_status = COALESCE(NULLIF(glp_status, ''), :glp_status),
                    species = COALESCE(NULLIF(species, ''), :species),
                    route = COALESCE(NULLIF(route, ''), :route),
                    main_source_document_id = COALESCE(main_source_document_id, :source_document_id),
                    module4_section = COALESCE(NULLIF(module4_section, ''), :module4_section),
                    extra_attributes = COALESCE(extra_attributes, '{}'::jsonb) || CAST(:extra_json AS jsonb),
                    updated_at = now()
                WHERE id = :study_id
                """
            ),
            {
                "pid": project_id,
                "study_number": study_number,
                "primary_study_number": primary_study_number,
                "study_type": study_type,
                "glp_status": glp_status or None,
                "species": species or None,
                "route": route or None,
                "source_document_id": source_document_id,
                "module4_section": module4_section,
                "extra_json": extra_json,
                "study_id": study_id,
            },
        )
        db.commit()
        return source_document_id, study_id

    study_id = db.execute(
        sqltext(
            """
            INSERT INTO ncd_study (
                project_id,
                sponsor_study_id,
                study_type,
                glp_status,
                species,
                route,
                main_source_document_id,
                module4_section,
                extra_attributes
            )
            VALUES (
                :pid,
                :primary_study_number,
                :study_type,
                :glp_status,
                :species,
                :route,
                :source_document_id,
                :module4_section,
                CAST(:extra_json AS jsonb)
            )
            RETURNING id
            """
        ),
        {
            "pid": project_id,
            "study_number": study_number,
            "primary_study_number": primary_study_number,
            "study_type": study_type,
            "glp_status": glp_status or None,
            "species": species or None,
            "route": route or None,
            "source_document_id": source_document_id,
            "module4_section": module4_section,
            "extra_json": extra_json,
        },
    ).scalar()
    if study_id is None:
        raise RuntimeError("Failed to insert ncd_study record for pharmacology overview")
    db.commit()
    return source_document_id, str(study_id)


class DummyLLM(LLMClient):
    """Deterministic stub for tox pipeline testing (no external calls)."""

    def extract_json(self, system_prompt: str, user_prompt: str, response_model=None):  # type: ignore[override]
        lower_prompt = f"{system_prompt} {user_prompt}".lower()
        if "pk" in lower_prompt or "cmax" in lower_prompt or "auc" in lower_prompt:
            return {
                "study_id": "demo",
                "species": "rat",
                "route": "oral",
                "parameters": [
                    {
                        "dose_group_name": "High",
                        "parameter": "Cmax",
                        "value": 123.4,
                        "unit": "ng/mL",
                        "timepoint": "Day 1",
                        "clinical_multiple": 4.2,
                    },
                    {
                        "dose_group_name": "High",
                        "parameter": "AUC",
                        "value": 9876.5,
                        "unit": "ng*h/mL",
                        "timepoint": "Day 28",
                        "clinical_multiple": 3.8,
                    },
                ],
                "source_chunk_ids": [],
            }

        return {
            "study_id": "demo",
            "species": "rat",
            "route": "oral",
            "duration_days": 28,
            "noael_mg_per_kg": 50,
            "loael_mg_per_kg": 100,
            "limiting_organ": "liver",
            "limiting_finding": "ALT increase",
            "clinical_multiple": 5,
            "dose_groups": [
                {
                    "name": "Low",
                    "sex": "M/F",
                    "n_animals": 10,
                    "dose_mg_per_kg": 10,
                    "dose_mg_per_m2": None,
                },
                {
                    "name": "High",
                    "sex": "M/F",
                    "n_animals": 10,
                    "dose_mg_per_kg": 50,
                    "dose_mg_per_m2": None,
                },
            ],
            "exposure_metrics": [
                {
                    "dose_group_name": "High",
                    "parameter": "Cmax",
                    "value": 123.4,
                    "unit": "ng/mL",
                    "timepoint": "Day 1",
                    "clinical_multiple": 4.2,
                },
                {
                    "dose_group_name": "High",
                    "parameter": "AUC",
                    "value": 9876.5,
                    "unit": "ng*h/mL",
                    "timepoint": "Day 28",
                    "clinical_multiple": 3.8,
                },
            ],
            "findings": [
                {
                    "organ_system": "Hepatic",
                    "organ": "Liver",
                    "finding_term": "ALT increase",
                    "severity": "mild",
                    "adverse": False,
                    "reversible": True,
                    "onset_day": 7,
                    "dose_threshold_mg_per_kg": 50,
                    "noael_flag": False,
                }
            ],
            "source_chunk_ids": [],
        }

    def generate_text(self, system_prompt: str, user_prompt: str) -> str:  # type: ignore[override]
        return "repeat_dose_tox"


_OPENAI_RATE_LIMIT_LOCK = threading.Lock()
_OPENAI_NEXT_ALLOWED_AT = 0.0
_OPENAI_CALL_SEMAPHORE = threading.Semaphore(1)
_OPENAI_SEMAPHORE_SIZE = 1
_OPENAI_SEMAPHORE_LOCK = threading.Lock()


def _is_rate_limit_error(exc: Exception) -> bool:
    message = str(exc).lower()
    if "too many requests" in message or "rate limit" in message or "429" in message:
        return True
    try:
        from openai import RateLimitError

        return isinstance(exc, RateLimitError)
    except Exception:
        return False


def _is_model_unavailable_error(exc: Exception) -> bool:
    message = str(exc).lower()
    signals = (
        "model_not_found",
        "does not exist or you do not have access",
        "you do not have access to this model",
        "unknown model",
        "invalid model",
    )
    return any(signal in message for signal in signals)


def _extract_retry_after_seconds(exc: Exception) -> Optional[float]:
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers:
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except (TypeError, ValueError):
                    pass

    message = str(exc)
    match = _TRY_AGAIN_IN_SECONDS_PATTERN.search(message)
    if not match:
        return None
    try:
        return max(0.0, float(match.group(1)))
    except (TypeError, ValueError):
        return None


def _configure_openai_limiters(max_concurrent: int) -> None:
    global _OPENAI_CALL_SEMAPHORE, _OPENAI_SEMAPHORE_SIZE
    desired = max(1, int(max_concurrent))
    with _OPENAI_SEMAPHORE_LOCK:
        if desired == _OPENAI_SEMAPHORE_SIZE:
            return
        _OPENAI_CALL_SEMAPHORE = threading.Semaphore(desired)
        _OPENAI_SEMAPHORE_SIZE = desired


class ThrottledLLM(LLMClient):
    """Thin wrapper that serializes OpenAI calls + retries on 429s."""

    def __init__(
        self,
        base: LLMClient,
        *,
        fallback_models: Optional[Sequence[str]] = None,
        min_interval_seconds: float = 0.0,
        max_retries: int = 3,
        initial_backoff_seconds: float = 2.0,
        max_backoff_seconds: float = 12.0,
        jitter_seconds: float = 0.5,
        max_retry_after_seconds: float = 12.0,
        max_total_retry_seconds: float = 45.0,
    ):
        self._base = base
        fallback = [str(model).strip() for model in (fallback_models or [])]
        deduped_models: List[str] = []
        seen: Set[str] = set()
        for candidate in [base.model_name, *fallback]:
            name = str(candidate or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped_models.append(name)
        self._model_candidates = deduped_models or [base.model_name]
        self.model_name = self._model_candidates[0]
        self._base.model_name = self.model_name
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.max_retries = max(0, max_retries)
        self.initial_backoff_seconds = max(0.0, initial_backoff_seconds)
        self.max_backoff_seconds = max(0.0, max_backoff_seconds)
        self.jitter_seconds = max(0.0, jitter_seconds)
        self.max_retry_after_seconds = max(0.0, max_retry_after_seconds)
        self.max_total_retry_seconds = max(0.0, max_total_retry_seconds)

    def _switch_model(self, model_name: str) -> None:
        self.model_name = model_name
        self._base.model_name = model_name

    def _wait_for_turn(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        global _OPENAI_NEXT_ALLOWED_AT
        while True:
            with _OPENAI_RATE_LIMIT_LOCK:
                now = time.monotonic()
                if now >= _OPENAI_NEXT_ALLOWED_AT:
                    _OPENAI_NEXT_ALLOWED_AT = now + self.min_interval_seconds
                    return
                sleep_for = _OPENAI_NEXT_ALLOWED_AT - now
            if sleep_for > 0:
                time.sleep(sleep_for)

    def _call_with_retry(self, call: Callable[[], Any]) -> Any:
        attempt = 0
        total_backoff = 0.0
        attempted_models: Set[str] = {self.model_name.lower()}
        while True:
            with _OPENAI_CALL_SEMAPHORE:
                self._wait_for_turn()
                try:
                    return call()
                except Exception as exc:
                    is_rate_limit = _is_rate_limit_error(exc)
                    is_model_unavailable = _is_model_unavailable_error(exc)
                    if is_rate_limit or is_model_unavailable:
                        next_model = next(
                            (
                                candidate
                                for candidate in self._model_candidates
                                if candidate.lower() not in attempted_models
                            ),
                            None,
                        )
                        if next_model:
                            attempted_models.add(next_model.lower())
                            previous = self.model_name
                            self._switch_model(next_model)
                            attempt = 0
                            total_backoff = 0.0
                            reason = (
                                "rate limit"
                                if is_rate_limit
                                else "model unavailable"
                            )
                            print(
                                f"[WARN] OpenAI {reason} for '{previous}'; "
                                f"switching to fallback model '{next_model}'",
                                file=sys.stderr,
                            )
                            continue
                    if not is_rate_limit or attempt >= self.max_retries:
                        raise
                    retry_after = _extract_retry_after_seconds(exc)
                    backoff = min(
                        self.max_backoff_seconds,
                        self.initial_backoff_seconds * (2 ** attempt),
                    )
                    if retry_after is not None:
                        retry_after_delay = retry_after + 0.25
                        if self.max_retry_after_seconds > 0:
                            retry_after_delay = min(
                                retry_after_delay,
                                self.max_retry_after_seconds,
                            )
                        backoff = max(backoff, retry_after_delay)
                    if self.jitter_seconds > 0:
                        backoff += random.uniform(0, self.jitter_seconds)
                    if (
                        self.max_total_retry_seconds > 0
                        and total_backoff + backoff > self.max_total_retry_seconds
                    ):
                        print(
                            f"[WARN] OpenAI retry budget exceeded for '{self.model_name}' "
                            f"({total_backoff + backoff:.2f}s > "
                            f"{self.max_total_retry_seconds:.2f}s); aborting retries",
                            file=sys.stderr,
                        )
                        raise
                    print(
                        f"[WARN] OpenAI rate limit for '{self.model_name}' "
                        f"(attempt {attempt + 1}/{self.max_retries + 1}); "
                        f"retrying in {backoff:.2f}s",
                        file=sys.stderr,
                    )
            time.sleep(backoff)
            total_backoff += backoff
            attempt += 1

    def extract_json(self, system_prompt: str, user_prompt: str, response_model=None):  # type: ignore[override]
        return self._call_with_retry(
            lambda: self._base.extract_json(system_prompt, user_prompt, response_model)
        )

    def generate_text(self, system_prompt: str, user_prompt: str) -> str:  # type: ignore[override]
        return str(
            self._call_with_retry(
                lambda: self._base.generate_text(system_prompt, user_prompt)
            )
        )


def _build_llm_client(mode: str, args: argparse.Namespace) -> LLMClient:
    base = _resolve_llm(mode)
    if isinstance(base, DummyLLM):
        return base
    fallback_models = _parse_csv_models(getattr(args, "llm_fallback_models", None))
    return ThrottledLLM(
        base=base,
        fallback_models=fallback_models,
        min_interval_seconds=args.llm_min_interval_seconds,
        max_retries=args.llm_max_retries,
        initial_backoff_seconds=args.llm_initial_backoff_seconds,
        max_backoff_seconds=args.llm_max_backoff_seconds,
        jitter_seconds=args.llm_jitter_seconds,
        max_retry_after_seconds=args.llm_max_retry_after_seconds,
        max_total_retry_seconds=args.llm_max_total_retry_seconds,
    )


def _module_number(segment: str) -> Optional[int]:
    match = _MODULE_PATTERN.match(segment.strip())
    if not match:
        return None
    try:
        return int(match.group("number"))
    except (TypeError, ValueError):
        return None


def _normalize_version_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    value_str = str(value).strip()
    if not value_str or value_str.lower() == "null":
        return None
    return value_str


def _to_iso(dt: Any) -> Optional[str]:
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).isoformat()
    return None


def _is_pdf_key(key: str) -> bool:
    return key.lower().endswith(".pdf") and not key.endswith("/")


def _iter_latest_pdf_versions(
    client: Any, bucket: str, prefix: str
) -> Iterator[Dict[str, Any]]:
    try:
        paginator = client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for version in page.get("Versions", []):
                if not version.get("IsLatest"):
                    continue
                key = version.get("Key")
                if not key or not _is_pdf_key(key):
                    continue
                yield {
                    "key": key,
                    "version_id": _normalize_version_id(version.get("VersionId")),
                    "last_modified": _to_iso(version.get("LastModified")),
                }
        return
    except ClientError as exc:
        print(
            f"[WARN] list_object_versions failed ({exc}); falling back to list_objects_v2.",
            file=sys.stderr,
        )

    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key")
            if not key or not _is_pdf_key(key):
                continue
            yield {
                "key": key,
                "version_id": None,
                "last_modified": _to_iso(obj.get("LastModified")),
            }


def _iter_module_pdfs(
    client: Any,
    bucket: str,
    prefix: str,
    module_number: int,
) -> Iterator[Dict[str, Any]]:
    for obj in _iter_latest_pdf_versions(client, bucket, prefix):
        parts = obj["key"].split("/")
        if len(parts) < 3:
            continue
        mod_num = _module_number(parts[2])
        if mod_num != module_number:
            continue
        yield obj


def _collect_candidate_docs(
    client: Any, args: argparse.Namespace, prefix: str
) -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []
    iterator = (
        _iter_latest_pdf_versions(client, args.bucket, prefix)
        if args.all_modules
        else _iter_module_pdfs(client, args.bucket, prefix, args.module)
    )
    for obj in iterator:
        if args.limit is not None and len(docs) >= args.limit:
            break
        docs.append(obj)
    return docs


def _format_error(exc: Exception, limit: int = 2000) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if len(message) > limit:
        return f"{message[:limit - 3]}..."
    return message


def _s3_object_exists(client: Any, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "404",
            "NoSuchKey",
            "NotFound",
        }:
            return False
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return False
        raise


def _download_s3_pdf(
    client: Any, bucket: str, key: str, version_id: Optional[str]
) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    extra_args = {"VersionId": version_id} if version_id else None
    client.download_file(
        Bucket=bucket, Key=key, Filename=tmp.name, ExtraArgs=extra_args
    )
    return Path(tmp.name)


def _has_tox_data(repo: NCDRepository, project_id: str, file_name: str) -> bool:
    row = repo.session.execute(
        sqltext(
            """
            SELECT EXISTS (
                SELECT 1
                FROM ncd_source_document sd
                JOIN ncd_study s ON s.main_source_document_id = sd.id
                LEFT JOIN ncd_finding f ON f.study_id = s.id
                LEFT JOIN ncd_exposure_metric em ON em.study_id = s.id
                LEFT JOIN ncd_study_safety_summary ss ON ss.study_id = s.id
                WHERE sd.project_id = :pid
                  AND sd.file_name = :fname
                  AND (f.id IS NOT NULL OR em.id IS NOT NULL OR ss.id IS NOT NULL)
            ) AS has_data
            """
        ),
        {"pid": project_id, "fname": file_name},
    ).scalar()
    return bool(row)


def _has_pk_data(repo: NCDRepository, project_id: str, file_name: str) -> bool:
    row = repo.session.execute(
        sqltext(
            """
            SELECT EXISTS (
                SELECT 1
                FROM ncd_source_document sd
                JOIN ncd_study s ON s.main_source_document_id = sd.id
                JOIN ncd_exposure_metric em ON em.study_id = s.id
                WHERE sd.project_id = :pid
                  AND sd.file_name = :fname
            ) AS has_data
            """
        ),
        {"pid": project_id, "fname": file_name},
    ).scalar()
    return bool(row)


def _resolve_llm(mode: str) -> LLMClient:
    if mode == "dummy":
        return DummyLLM()
    if mode == "real":
        return LLMClient()
    raise ValueError("tox llm mode must be 'real' or 'dummy'")


def _parse_csv_models(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _core_needed(
    *,
    core_check: str,
    md_exists: bool,
    status_completed: bool,
    force: bool,
) -> bool:
    if force:
        return True
    if core_check == "md":
        return not md_exists
    if core_check == "status":
        return not status_completed
    if core_check == "both":
        return (not md_exists) or (not status_completed)
    return False


def _parse_company_project(key: str) -> tuple[Optional[str], Optional[str]]:
    parts = key.split("/")
    if len(parts) >= 2:
        return parts[0], parts[1]
    if parts:
        return parts[0], None
    return None, None


def _key_after_prefix(key: str, prefix: str) -> str:
    normalized = prefix.strip("/")
    if not normalized:
        return key
    with_slash = f"{normalized}/"
    if key.startswith(with_slash):
        return key[len(with_slash) :]
    if key.startswith(normalized):
        return key[len(normalized) :].lstrip("/")
    return key


def _project_root_prefix(args: argparse.Namespace, normalized_prefix: str) -> str:
    company = (args.company or "").strip("/")
    project = (args.project or "").strip("/")
    if company and project:
        return f"{company}/{project}/"

    parts = [part for part in normalized_prefix.strip("/").split("/") if part]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}/"
    return normalized_prefix


def _display_key_for_logs(key: str, args: argparse.Namespace) -> str:
    project_prefix = str(getattr(args, "project_root_prefix", "") or "")
    if project_prefix:
        relative = _key_after_prefix(key, project_prefix)
        if relative and relative != key:
            return relative

    run_prefix = str(getattr(args, "run_prefix", "") or "")
    if run_prefix:
        relative = _key_after_prefix(key, run_prefix)
        if relative and relative != key:
            return relative

    return key


def _status_is_completed(status: str) -> bool:
    return status.strip().lower() == "completed"


def _build_already_ingested_entry(
    obj: Dict[str, Any],
    *,
    args: argparse.Namespace,
    display_key: str,
    md_exists: bool,
    status_completed: bool,
    tox_data_exists: bool,
    pk_data_exists: bool,
    core_status: str,
    langchain_status: str,
    context_status: str,
    summary_status: str,
    pharm_overview_required: bool,
    pharm_overview_status: str,
    tox_status: str,
    pk_status: str,
) -> Dict[str, Any]:
    return {
        "key": obj.get("key"),
        "display_key": display_key,
        "version_id": obj.get("version_id"),
        "last_modified": obj.get("last_modified"),
        "md_exists": md_exists,
        "status_completed": status_completed,
        "tox_data_exists": tox_data_exists,
        "pk_data_exists": pk_data_exists,
        "core_required": False,
        "core_status": core_status or "completed",
        "core_error": "",
        "core_duration_seconds": 0.0,
        "langchain_required": False,
        "langchain_status": langchain_status or "completed",
        "langchain_error": "",
        "langchain_duration_seconds": 0.0,
        "context_required": False,
        "context_status": context_status or "completed",
        "context_error": "",
        "context_duration_seconds": 0.0,
        "summary_required": False,
        "summary_status": summary_status or "completed",
        "summary_error": "",
        "summary_duration_seconds": 0.0,
        "summary_count": 0,
        "pharm_overview_required": pharm_overview_required,
        "pharm_overview_status": pharm_overview_status or "skipped",
        "pharm_overview_error": "",
        "pharm_overview_duration_seconds": 0.0,
        "pharm_overview_source_document_id": "",
        "pharm_overview_study_id": "",
        "pharm_overview_study_number": "",
        "pharm_overview_module4_section": "",
        "document_version_id": "",
        "tox_required": False,
        "tox_status": tox_status or "completed",
        "tox_error": "",
        "tox_duration_seconds": 0.0,
        "tox_source_document_id": "",
        "tox_study_id": "",
        "pk_required": False,
        "pk_status": pk_status or "completed",
        "pk_error": "",
        "pk_duration_seconds": 0.0,
        "pk_source_document_id": "",
        "pk_study_id": "",
        "meta_key": "",
    }


def _is_fully_ingested(
    obj: Dict[str, Any],
    *,
    args: argparse.Namespace,
    s3_client: Any,
    repo: NCDRepository,
) -> tuple[bool, Dict[str, Any]]:
    key = str(obj.get("key") or "")
    version_id = obj.get("version_id")
    file_name = Path(key).name
    display_key = _display_key_for_logs(key, args)

    md_key = f"{key}{args.md_suffix}"
    quality_key = f"{key}.quality.json"
    meta_key = f"{key}.meta.json"
    module4_section = _extract_module4_section_from_key(key)
    pharm_overview_applicable = bool(module4_section) and bool(args.pharm_overview)

    md_exists = _s3_object_exists(s3_client, args.bucket, md_key)
    quality_exists = _s3_object_exists(s3_client, args.bucket, quality_key)
    meta_exists = True if args.no_meta else _s3_object_exists(s3_client, args.bucket, meta_key)

    core_status = "missing"
    langchain_status = "missing"
    context_status = "missing"
    summary_status = "missing"
    pharm_overview_status = "missing" if pharm_overview_applicable else "skipped"
    tox_status = "missing"
    pk_status = "missing"
    tox_data_exists = False
    pk_data_exists = False

    core_row = repo.fetch_pipeline_status_for_key(
        s3_bucket=args.bucket,
        s3_key=key,
        s3_version_id=version_id,
        pipeline="core",
    )
    if core_row:
        core_status = str(core_row.get("status") or core_status)

    if not args.skip_langchain:
        langchain_row = repo.fetch_pipeline_status_for_key(
            s3_bucket=args.bucket,
            s3_key=key,
            s3_version_id=version_id,
            pipeline="langchain",
        )
        if langchain_row:
            langchain_status = str(langchain_row.get("status") or langchain_status)

    if args.context:
        context_row = repo.fetch_pipeline_status_for_key(
            s3_bucket=args.bucket,
            s3_key=key,
            s3_version_id=version_id,
            pipeline="context",
        )
        if context_row:
            context_status = str(context_row.get("status") or context_status)

    if args.section_summary:
        summary_row = repo.fetch_pipeline_status_for_key(
            s3_bucket=args.bucket,
            s3_key=key,
            s3_version_id=version_id,
            pipeline="section-summary",
        )
        if summary_row:
            summary_status = str(summary_row.get("status") or summary_status)

    if pharm_overview_applicable:
        pharm_overview_row = repo.fetch_pipeline_status_for_key(
            s3_bucket=args.bucket,
            s3_key=key,
            s3_version_id=version_id,
            pipeline="pharm-overview",
        )
        if pharm_overview_row:
            pharm_overview_status = str(
                pharm_overview_row.get("status") or pharm_overview_status
            )

    if args.mode in {"auto", "tox"}:
        tox_row = repo.fetch_pipeline_status_for_key(
            s3_bucket=args.bucket,
            s3_key=key,
            s3_version_id=version_id,
            pipeline="tox",
        )
        if tox_row:
            tox_status = str(tox_row.get("status") or tox_status)
        if args.project_id:
            tox_data_exists = _has_tox_data(repo, args.project_id, file_name)
            pk_data_exists = _has_pk_data(repo, args.project_id, file_name)
            pk_status = tox_status
    elif args.mode == "pk":
        pk_row = repo.fetch_pipeline_status_for_key(
            s3_bucket=args.bucket,
            s3_key=key,
            s3_version_id=version_id,
            pipeline="tox",
        )
        if pk_row:
            pk_status = str(pk_row.get("status") or pk_status)
        if args.project_id:
            pk_data_exists = _has_pk_data(repo, args.project_id, file_name)

    core_outputs_complete = md_exists and quality_exists and meta_exists
    core_pipeline_complete = _status_is_completed(core_status)
    if not args.skip_langchain:
        core_pipeline_complete = core_pipeline_complete and _status_is_completed(
            langchain_status
        )
    if args.context:
        core_pipeline_complete = core_pipeline_complete and _status_is_completed(
            context_status
        )
    if args.section_summary:
        core_pipeline_complete = core_pipeline_complete and _status_is_completed(
            summary_status
        )
    if pharm_overview_applicable:
        core_pipeline_complete = core_pipeline_complete and _status_is_completed(
            pharm_overview_status
        )
    core_complete = core_outputs_complete and core_pipeline_complete

    tox_complete = _status_is_completed(tox_status) and tox_data_exists
    pk_complete = _status_is_completed(pk_status) and pk_data_exists

    # Force flags must bypass "already ingested" guard for the forced stages.
    if bool(args.force or args.force_core):
        core_complete = False
    if (not args.skip_langchain) and bool(args.force_langchain):
        core_complete = False
    if bool(args.context) and bool(args.force_context):
        core_complete = False
    if bool(args.section_summary) and bool(args.force_section_summary):
        core_complete = False
    if pharm_overview_applicable and bool(args.force_pharm_overview):
        core_complete = False
    if bool(args.force_tox):
        tox_complete = False
    if bool(args.force_pk):
        pk_complete = False

    if args.mode == "core":
        fully_ingested = core_complete
    elif args.mode == "tox":
        fully_ingested = tox_complete
    elif args.mode == "pk":
        fully_ingested = pk_complete
    else:
        fully_ingested = core_complete and tox_complete

    status_completed = _status_is_completed(core_status)
    state = _build_already_ingested_entry(
        obj,
        args=args,
        display_key=display_key,
        md_exists=md_exists,
        status_completed=status_completed,
        tox_data_exists=tox_data_exists,
        pk_data_exists=pk_data_exists,
        core_status=core_status,
        langchain_status=langchain_status,
        context_status=context_status,
        summary_status=summary_status,
        pharm_overview_required=pharm_overview_applicable,
        pharm_overview_status=pharm_overview_status,
        tox_status=tox_status,
        pk_status=pk_status,
    )
    return fully_ingested, state


def _ensure_project_id(
    repo: NCDRepository,
    *,
    project_name: str,
    tenant_id: Optional[str],
    created_by: Optional[str],
    product_type: str,
    sponsor_email: str,
    sponsor_name: Optional[str],
) -> str:
    db = repo.session
    row = db.execute(
        sqltext(
            """
            SELECT id
            FROM projects
            WHERE ind_title = :name OR drug_name = :name
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"name": project_name},
    ).scalar()
    if row:
        return str(row)

    values = {
        "ind_title": project_name,
        "drug_name": project_name,
        "product_type": product_type,
        "sponsor_contact_email": sponsor_email,
        "sponsor_name": sponsor_name,
        "project_creator_id": created_by,
        "tenantid": tenant_id,
    }
    row = db.execute(
        sqltext(
            """
            INSERT INTO projects (
                ind_title,
                drug_name,
                product_type,
                sponsor_contact_email,
                sponsor_name,
                project_creator_id,
                tenantid
            )
            VALUES (
                :ind_title,
                :drug_name,
                :product_type,
                :sponsor_contact_email,
                :sponsor_name,
                :project_creator_id,
                :tenantid
            )
            RETURNING id
            """
        ),
        values,
    ).scalar()
    if not row:
        raise RuntimeError("Failed to create project row")
    db.commit()
    return str(row)


def _read_markdown_sidecar(
    client: Any,
    bucket: str,
    key: str,
) -> Optional[str]:
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "NotFound"}:
            return None
        raise
    body = obj.get("Body")
    if not body:
        return None
    return body.read().decode("utf-8")


def _upload_meta_json(
    client: Any,
    bucket: str,
    key: str,
    payload: Dict[str, Any],
) -> Optional[str]:
    meta_key = f"{key}.meta.json"
    try:
        client.put_object(
            Bucket=bucket,
            Key=meta_key,
            Body=json.dumps(payload, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except ClientError as exc:
        print(f"[WARN] Failed to upload meta json for {key}: {exc}", file=sys.stderr)
        return None
    return meta_key


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch ingest Module 4 PDFs from S3 into the DB."
    )
    parser.add_argument(
        "--bucket", default=os.getenv("S3_BUCKET"), help="S3 bucket name."
    )
    parser.add_argument(
        "--company", default=os.getenv("COMPANY", "longooc.com"), help="Company prefix."
    )
    parser.add_argument(
        "--project", default=os.getenv("PROJECT"), help="Project name under company."
    )
    parser.add_argument(
        "--project-id",
        default=os.getenv("PROJECT_ID"),
        help="Project UUID for NCD tox pipeline.",
    )
    parser.add_argument(
        "--module", type=int, default=4, help="Module number to ingest."
    )
    parser.add_argument(
        "--all-modules",
        action="store_true",
        help="Process all PDFs under the prefix (ignore module filtering).",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help="Override S3 prefix (defaults to <company>/<project>/).",
    )
    parser.add_argument(
        "--aws-region", default=os.getenv("AWS_REGION"), help="AWS region override."
    )
    parser.add_argument(
        "--tenant-id", default=os.getenv("DEFAULT_TENANT_ID"), help="Tenant UUID."
    )
    parser.add_argument(
        "--created-by", default=os.getenv("DEFAULT_USER_ID"), help="User UUID."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Limit on number of PDFs to process."
    )
    parser.add_argument(
        "--workers", type=int, default=4, help="Concurrent worker threads."
    )
    parser.add_argument(
        "--llm-max-concurrent",
        type=int,
        default=int(os.getenv("INGEST_LLM_MAX_CONCURRENT", "1")),
        help="Maximum concurrent OpenAI calls across all workers.",
    )
    parser.add_argument(
        "--llm-min-interval-seconds",
        type=float,
        default=float(os.getenv("INGEST_LLM_MIN_INTERVAL_SECONDS", "0")),
        help=(
            "Minimum delay between OpenAI calls across all workers. "
            "Set >0 (e.g. 1.0-2.0) to reduce 429s."
        ),
    )
    parser.add_argument(
        "--llm-max-retries",
        type=int,
        default=int(os.getenv("INGEST_LLM_MAX_RETRIES", "3")),
        help="Max retries for OpenAI 429 rate-limit errors.",
    )
    parser.add_argument(
        "--llm-initial-backoff-seconds",
        type=float,
        default=float(os.getenv("INGEST_LLM_INITIAL_BACKOFF_SECONDS", "2")),
        help="Initial retry backoff seconds for OpenAI 429 errors.",
    )
    parser.add_argument(
        "--llm-max-backoff-seconds",
        type=float,
        default=float(os.getenv("INGEST_LLM_MAX_BACKOFF_SECONDS", "12")),
        help="Maximum retry backoff seconds for OpenAI 429 errors.",
    )
    parser.add_argument(
        "--llm-max-retry-after-seconds",
        type=float,
        default=float(os.getenv("INGEST_LLM_MAX_RETRY_AFTER_SECONDS", "12")),
        help=(
            "Cap for Retry-After based delays from OpenAI 429 responses. "
            "Set to 0 to disable this cap."
        ),
    )
    parser.add_argument(
        "--llm-max-total-retry-seconds",
        type=float,
        default=float(os.getenv("INGEST_LLM_MAX_TOTAL_RETRY_SECONDS", "45")),
        help=(
            "Maximum cumulative retry sleep time per OpenAI call before failing fast. "
            "Set to 0 to disable this cap."
        ),
    )
    parser.add_argument(
        "--llm-jitter-seconds",
        type=float,
        default=float(os.getenv("INGEST_LLM_JITTER_SECONDS", "0.5")),
        help="Random jitter added to OpenAI retry backoff.",
    )
    parser.add_argument(
        "--llm-fallback-models",
        default=os.getenv("LLM_FALLBACK_MODELS", ""),
        help=(
            "Comma-separated fallback models used when the active model hits rate limits "
            "(e.g. gpt-4.1-mini,gpt-4o-mini)."
        ),
    )
    parser.add_argument("--mode", choices=("auto", "core", "tox", "pk"), default="core")
    parser.add_argument(
        "--core-check", choices=("md", "status", "both"), default="both"
    )
    parser.add_argument(
        "--md-suffix", default=".extracted.md", help="Markdown sidecar suffix."
    )
    parser.add_argument(
        "--skip-langchain",
        action="store_true",
        default=True,
        help="Skip LangChain pipeline even when core runs.",
    )
    parser.add_argument(
        "--run-langchain",
        action="store_false",
        dest="skip_langchain",
        help="Enable LangChain pipeline (overrides default skip).",
    )
    parser.add_argument(
        "--force-langchain",
        action="store_true",
        help="Re-run LangChain pipeline even if completed.",
    )
    parser.add_argument(
        "--core-text-engines",
        default=None,
        help="Comma-separated text engines for core pipeline (e.g., pymupdf,pdfminer).",
    )
    parser.add_argument(
        "--core-table-engines",
        default=None,
        help="Comma-separated table engines for core pipeline (e.g., pdfplumber,camelot).",
    )
    parser.add_argument(
        "--disable-core-tables",
        action="store_true",
        help="Disable table extraction in core pipeline (avoids camelot/tabula).",
    )
    parser.add_argument("--force", action="store_true", help="Alias for --force-core.")
    parser.add_argument(
        "--force-core",
        action="store_true",
        default=True,
        help="Re-run core pipeline regardless of checks.",
    )
    parser.add_argument(
        "--no-force-core",
        action="store_false",
        dest="force_core",
        help="Respect core prechecks (disable default force).",
    )
    parser.add_argument(
        "--force-tox",
        action="store_true",
        help="Re-run tox pipeline regardless of checks.",
    )
    parser.add_argument(
        "--force-pk",
        action="store_true",
        help="Re-run PK extraction regardless of checks.",
    )
    parser.add_argument(
        "--no-precheck",
        action="store_true",
        default=True,
        help="Disable ingestion status precheck.",
    )
    parser.add_argument(
        "--precheck",
        action="store_false",
        dest="no_precheck",
        help="Enable ingestion status precheck (overrides default no-precheck).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="List matches without processing."
    )
    parser.add_argument(
        "--tox-llm",
        choices=("real", "dummy"),
        default="real",
        help="LLM mode for tox pipeline.",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="Skip embeddings in tox pipeline.",
    )
    parser.add_argument(
        "--report-dir",
        default="tmp/ingestion_reports",
        help="Directory to write JSON/CSV reports.",
    )
    parser.add_argument(
        "--project-product-type",
        default=os.getenv("PROJECT_PRODUCT_TYPE", "drug"),
        help="Product type for project auto-creation.",
    )
    parser.add_argument(
        "--project-sponsor-email",
        default=os.getenv("PROJECT_SPONSOR_EMAIL", "unknown@longooc.com"),
        help="Sponsor contact email for project auto-creation.",
    )
    parser.add_argument(
        "--project-sponsor-name",
        default=os.getenv("PROJECT_SPONSOR_NAME"),
        help="Sponsor name for project auto-creation.",
    )
    parser.add_argument(
        "--section-summary",
        action="store_true",
        default=True,
        help="Generate per-section summaries + keywords and persist to document_section_summary.",
    )
    parser.add_argument(
        "--no-section-summary",
        action="store_false",
        dest="section_summary",
        help="Disable section summaries (overrides default on).",
    )
    parser.add_argument(
        "--force-section-summary",
        action="store_true",
        default=True,
        help="Re-run section summaries even if completed.",
    )
    parser.add_argument(
        "--no-force-section-summary",
        action="store_false",
        dest="force_section_summary",
        help="Respect summary prechecks (disable default force).",
    )
    parser.add_argument(
        "--pharm-overview",
        action="store_true",
        default=True,
        help=(
            "Extract and upsert pharmacology overview metadata for 4.2.1.x reports "
            "(supports 2.6.3.1 table population)."
        ),
    )
    parser.add_argument(
        "--no-pharm-overview",
        action="store_false",
        dest="pharm_overview",
        help="Disable pharmacology overview metadata extraction.",
    )
    parser.add_argument(
        "--force-pharm-overview",
        action="store_true",
        help="Re-run pharmacology overview metadata extraction regardless of status.",
    )
    parser.add_argument(
        "--context",
        action="store_true",
        default=True,
        help="Generate context assets + summary/conclusion passages.",
    )
    parser.add_argument(
        "--no-context",
        action="store_false",
        dest="context",
        help="Disable context extraction (overrides default on).",
    )
    parser.add_argument(
        "--force-context",
        action="store_true",
        default=True,
        help="Re-run context extraction even if completed.",
    )
    parser.add_argument(
        "--no-force-context",
        action="store_false",
        dest="force_context",
        help="Respect context prechecks (disable default force).",
    )
    parser.add_argument(
        "--summary-min-chars",
        type=int,
        default=None,
        help="Override minimum characters required for section summaries.",
    )
    parser.add_argument(
        "--summary-max-chars",
        type=int,
        default=None,
        help="Override maximum characters sent to the LLM per section.",
    )
    parser.add_argument(
        "--summary-keywords-min",
        type=int,
        default=None,
        help="Minimum keywords per section summary.",
    )
    parser.add_argument(
        "--summary-keywords-max",
        type=int,
        default=None,
        help="Maximum keywords per section summary.",
    )
    parser.add_argument(
        "--summary-purpose",
        default=None,
        help="Summary purpose label (default: ctd_2_6).",
    )
    parser.add_argument(
        "--summary-type",
        default=None,
        help="Summary type label (default: abstractive).",
    )
    parser.add_argument(
        "--no-meta",
        action="store_true",
        help="Skip uploading .meta.json sidecar files.",
    )
    return parser.parse_args(argv)


def _process_document(obj: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    key = obj["key"]
    display_key = _display_key_for_logs(key, args)
    version_id = obj.get("version_id")
    last_modified = obj.get("last_modified")
    file_name = Path(key).name
    md_key = f"{key}{args.md_suffix}"
    module4_section = _extract_module4_section_from_key(key)
    pharm_overview_applicable = bool(module4_section) and bool(args.pharm_overview)
    print(f"[PROCESSING] {display_key}")

    s3_client = _require_boto3().client("s3", region_name=args.aws_region)
    repo: Optional[NCDRepository] = None
    md_exists = False
    status_completed = False
    tox_data_exists = False
    pk_data_exists = False

    core_required = False
    langchain_required = False
    tox_required = False
    pk_required = False
    summary_required = False
    context_required = False
    pharm_overview_required = False

    core_status = "skipped"
    langchain_status = "skipped"
    tox_status = "skipped"
    pk_status = "skipped"
    summary_status = "skipped"
    context_status = "skipped"
    pharm_overview_status = "skipped"
    core_error = ""
    langchain_error = ""
    tox_error = ""
    pk_error = ""
    summary_error = ""
    context_error = ""
    pharm_overview_error = ""
    core_duration = 0.0
    langchain_duration = 0.0
    tox_duration = 0.0
    pk_duration = 0.0
    summary_duration = 0.0
    context_duration = 0.0
    pharm_overview_duration = 0.0
    document_version_id = ""
    pharm_overview_source_document_id = ""
    pharm_overview_study_id = ""
    pharm_overview_study_number = ""
    pharm_overview_module4_section = module4_section or ""
    tox_source_document_id = ""
    tox_study_id = ""
    pk_source_document_id = ""
    pk_study_id = ""
    summary_count = 0
    meta_key = ""
    content_hash = ""

    try:
        precheck_enabled = not args.no_precheck

        if args.mode in {"auto", "core"}:
            try:
                md_exists = _s3_object_exists(s3_client, args.bucket, md_key)
            except Exception as exc:
                md_exists = False
                core_error = _format_error(exc)

            if precheck_enabled:
                try:
                    repo = repo or NCDRepository()
                    core_status_row = repo.fetch_pipeline_status_for_key(
                        s3_bucket=args.bucket,
                        s3_key=key,
                        s3_version_id=version_id,
                        pipeline="core",
                    )
                    langchain_status_row = repo.fetch_pipeline_status_for_key(
                        s3_bucket=args.bucket,
                        s3_key=key,
                        s3_version_id=version_id,
                        pipeline="langchain",
                    )
                    summary_status_row = repo.fetch_pipeline_status_for_key(
                        s3_bucket=args.bucket,
                        s3_key=key,
                        s3_version_id=version_id,
                        pipeline="section-summary",
                    )
                    pharm_overview_status_row = None
                    if pharm_overview_applicable:
                        pharm_overview_status_row = repo.fetch_pipeline_status_for_key(
                            s3_bucket=args.bucket,
                            s3_key=key,
                            s3_version_id=version_id,
                            pipeline="pharm-overview",
                        )
                    context_status_row = repo.fetch_pipeline_status_for_key(
                        s3_bucket=args.bucket,
                        s3_key=key,
                        s3_version_id=version_id,
                        pipeline="context",
                    )
                    status_completed = bool(
                        core_status_row and core_status_row.get("status") == "completed"
                    )
                    if core_status_row and core_status_row.get("content_hash"):
                        content_hash = str(core_status_row.get("content_hash") or "")
                    if core_status_row and core_status_row.get("document_version_id"):
                        document_version_id = str(
                            core_status_row.get("document_version_id")
                        )
                    if (
                        not document_version_id
                        and langchain_status_row
                        and langchain_status_row.get("document_version_id")
                    ):
                        document_version_id = str(
                            langchain_status_row.get("document_version_id")
                        )
                    if core_status_row:
                        core_status = str(core_status_row.get("status") or core_status)
                    if langchain_status_row:
                        langchain_status = str(
                            langchain_status_row.get("status") or langchain_status
                        )
                    if summary_status_row:
                        summary_status = str(
                            summary_status_row.get("status") or summary_status
                        )
                    if pharm_overview_status_row:
                        pharm_overview_status = str(
                            pharm_overview_status_row.get("status")
                            or pharm_overview_status
                        )
                    if context_status_row:
                        context_status = str(
                            context_status_row.get("status") or context_status
                        )
                except Exception as exc:
                    status_completed = False
                    core_error = _format_error(exc)

            core_required = _core_needed(
                core_check=args.core_check,
                md_exists=md_exists,
                status_completed=status_completed,
                force=bool(args.force or args.force_core),
            )

            if args.skip_langchain:
                langchain_required = False
            else:
                langchain_required = (
                    bool(args.force_langchain) or langchain_status != "completed"
                )

            if langchain_required and not document_version_id:
                core_required = True

            summary_required = bool(args.section_summary)
            if (
                summary_required
                and summary_status == "completed"
                and not args.force_section_summary
            ):
                summary_required = False
            if args.force_section_summary:
                summary_required = True

            pharm_overview_required = bool(pharm_overview_applicable)
            if (
                pharm_overview_required
                and pharm_overview_status == "completed"
                and not args.force_pharm_overview
            ):
                pharm_overview_required = False
            if args.force_pharm_overview and pharm_overview_applicable:
                pharm_overview_required = True

            context_required = bool(args.context)
            if (
                context_required
                and context_status == "completed"
                and not args.force_context
            ):
                context_required = False
            if args.force_context:
                context_required = True
            if summary_required and not document_version_id:
                core_required = True

        if args.mode in {"auto", "tox"}:
            if not args.project_id:
                raise RuntimeError("project_id is required for tox pipeline checks")
            try:
                repo = repo or NCDRepository()
                tox_status_row = repo.fetch_pipeline_status_for_key(
                    s3_bucket=args.bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    pipeline="tox",
                )
                if tox_status_row:
                    tox_status = str(tox_status_row.get("status") or tox_status)
                tox_data_exists = _has_tox_data(repo, args.project_id, file_name)
                pk_data_exists = _has_pk_data(repo, args.project_id, file_name)
                pk_status = tox_status
                tox_required = (
                    args.force_tox or tox_status != "completed" or not tox_data_exists
                )
            except Exception as exc:
                tox_data_exists = False
                pk_data_exists = False
                pk_status = "missing"
                tox_required = True
                tox_error = _format_error(exc)

        if args.mode == "pk":
            if not args.project_id:
                raise RuntimeError("project_id is required for pk pipeline checks")
            try:
                repo = repo or NCDRepository()
                pk_status_row = repo.fetch_pipeline_status_for_key(
                    s3_bucket=args.bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    pipeline="tox",
                )
                if pk_status_row:
                    pk_status = str(pk_status_row.get("status") or pk_status)
                pk_data_exists = _has_pk_data(repo, args.project_id, file_name)
                pk_required = (
                    args.force_pk or pk_status != "completed" or not pk_data_exists
                )
            except Exception as exc:
                pk_data_exists = False
                pk_required = True
                pk_error = _format_error(exc)

        if args.dry_run:
            return {
                "key": key,
                "version_id": version_id,
                "last_modified": last_modified,
                "md_exists": md_exists,
                "status_completed": status_completed,
                "tox_data_exists": tox_data_exists,
                "pk_data_exists": pk_data_exists,
                "core_required": core_required,
                "core_status": "dry_run",
                "core_error": core_error,
                "core_duration_seconds": core_duration,
                "langchain_required": langchain_required,
                "langchain_status": "dry_run",
                "langchain_error": langchain_error,
                "langchain_duration_seconds": langchain_duration,
                "summary_required": summary_required,
                "summary_status": "dry_run",
                "summary_error": summary_error,
                "summary_duration_seconds": summary_duration,
                "summary_count": summary_count,
                "pharm_overview_required": pharm_overview_required,
                "pharm_overview_status": (
                    "dry_run" if pharm_overview_required else pharm_overview_status
                ),
                "pharm_overview_error": pharm_overview_error,
                "pharm_overview_duration_seconds": pharm_overview_duration,
                "pharm_overview_source_document_id": pharm_overview_source_document_id,
                "pharm_overview_study_id": pharm_overview_study_id,
                "pharm_overview_study_number": pharm_overview_study_number,
                "pharm_overview_module4_section": pharm_overview_module4_section,
                "context_required": context_required,
                "context_status": "dry_run",
                "context_error": context_error,
                "context_duration_seconds": context_duration,
                "document_version_id": document_version_id,
                "tox_required": tox_required,
                "tox_status": "dry_run",
                "tox_error": tox_error,
                "tox_duration_seconds": tox_duration,
                "tox_source_document_id": tox_source_document_id,
                "tox_study_id": tox_study_id,
                "pk_required": pk_required,
                "pk_status": "dry_run",
                "pk_error": pk_error,
                "pk_duration_seconds": pk_duration,
                "pk_source_document_id": pk_source_document_id,
                "pk_study_id": pk_study_id,
                "meta_key": meta_key,
            }

        if (core_required or langchain_required or context_required) and args.mode in {
            "auto",
            "core",
        }:
            started = datetime.now(timezone.utc)
            try:
                result = process_message(
                    {
                        "bucket": args.bucket,
                        "key": key,
                        "version_id": version_id,
                        "tenant_id": args.tenant_id,
                        "created_by": args.created_by,
                        "project_id": args.project_id,
                    },
                    force=bool(args.force or args.force_core),
                    run_core=core_required,
                    run_langchain=langchain_required,
                    run_context=context_required,
                )
                core_status = result.get("core_status", core_status)
                langchain_status = result.get("langchain_status", langchain_status)
                context_status = result.get("context_status", context_status)
                core_error = result.get("core_error") or core_error
                langchain_error = result.get("langchain_error") or langchain_error
                context_error = result.get("context_error") or context_error
                core_duration = float(
                    result.get("core_duration_seconds") or core_duration
                )
                langchain_duration = float(
                    result.get("langchain_duration_seconds") or langchain_duration
                )
                context_duration = float(
                    result.get("context_duration_seconds") or context_duration
                )
                if result.get("document_version_id"):
                    document_version_id = str(result.get("document_version_id"))
            except Exception as exc:
                if core_required:
                    core_status = "failed"
                    core_error = _format_error(exc)
                if langchain_required:
                    langchain_status = "failed"
                    langchain_error = _format_error(exc)
                if context_required:
                    context_status = "failed"
                    context_error = _format_error(exc)
            core_duration = max(
                core_duration, (datetime.now(timezone.utc) - started).total_seconds()
            )
            if not content_hash:
                try:
                    repo = repo or NCDRepository()
                    status_row = repo.fetch_pipeline_status_for_key(
                        s3_bucket=args.bucket,
                        s3_key=key,
                        s3_version_id=version_id,
                        pipeline="core",
                    )
                    if status_row and status_row.get("content_hash"):
                        content_hash = str(status_row.get("content_hash") or "")
                except Exception:
                    pass

        if tox_required and args.mode in {"auto", "tox"}:
            started = datetime.now(timezone.utc)
            local_pdf = None
            try:
                run_pk_with_tox = args.mode == "auto"
                if run_pk_with_tox:
                    pk_required = True
                llm_client = _build_llm_client(args.tox_llm, args)
                local_pdf = _download_s3_pdf(s3_client, args.bucket, key, version_id)
                content_hash = sha256_file(local_pdf)
                repo = repo or NCDRepository()
                repo.upsert_pipeline_status(
                    s3_bucket=args.bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    content_hash=content_hash,
                    pipeline="tox",
                    status="processing",
                )
                tox_result = run_pdf_ingest_and_extract(
                    pdf_path=local_pdf,
                    project_id=args.project_id,
                    module="Module 4",
                    llm_client=llm_client,
                    embed=not args.no_embed,
                    run_tox=True,
                    run_pk=run_pk_with_tox,
                )
                tox_status = "completed"
                tox_source_document_id = str(tox_result.get("source_document_id", ""))
                tox_study_id = str(tox_result.get("study_id", ""))
                tox_data_exists = True
                if run_pk_with_tox:
                    pk_status = "completed"
                    pk_source_document_id = tox_source_document_id
                    pk_study_id = tox_study_id
                    pk_data_exists = True
                repo.upsert_pipeline_status(
                    s3_bucket=args.bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    content_hash=content_hash,
                    pipeline="tox",
                    status="completed",
                    source_document_id=tox_source_document_id,
                    study_id=tox_study_id,
                )
            except Exception as exc:
                tox_status = "failed"
                tox_error = _format_error(exc)
                if args.mode == "auto":
                    pk_status = "failed"
                    pk_error = _format_error(exc)
                if local_pdf:
                    try:
                        content_hash = sha256_file(local_pdf)
                        repo = repo or NCDRepository()
                        repo.upsert_pipeline_status(
                            s3_bucket=args.bucket,
                            s3_key=key,
                            s3_version_id=version_id,
                            content_hash=content_hash,
                            pipeline="tox",
                            status="failed",
                            error_message=tox_error,
                        )
                    except Exception:
                        pass
            finally:
                if local_pdf and local_pdf.exists():
                    try:
                        local_pdf.unlink()
                    except Exception:
                        pass
            tox_duration = (datetime.now(timezone.utc) - started).total_seconds()
            if args.mode == "auto":
                pk_duration = tox_duration

        if pk_required and args.mode == "pk":
            started = datetime.now(timezone.utc)
            local_pdf = None
            try:
                llm_client = _build_llm_client(args.tox_llm, args)
                local_pdf = _download_s3_pdf(s3_client, args.bucket, key, version_id)
                content_hash = sha256_file(local_pdf)
                repo = repo or NCDRepository()
                repo.upsert_pipeline_status(
                    s3_bucket=args.bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    content_hash=content_hash,
                    pipeline="tox",
                    status="processing",
                )
                pk_result = run_pdf_ingest_and_extract(
                    pdf_path=local_pdf,
                    project_id=args.project_id,
                    module="Module 4",
                    llm_client=llm_client,
                    embed=not args.no_embed,
                    run_tox=False,
                    run_pk=True,
                )
                pk_status = "completed"
                pk_source_document_id = str(pk_result.get("source_document_id", ""))
                pk_study_id = str(pk_result.get("study_id", ""))
                pk_data_exists = True
                repo.upsert_pipeline_status(
                    s3_bucket=args.bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    content_hash=content_hash,
                    pipeline="tox",
                    status="completed",
                    source_document_id=pk_source_document_id,
                    study_id=pk_study_id,
                )
            except Exception as exc:
                pk_status = "failed"
                pk_error = _format_error(exc)
                if local_pdf:
                    try:
                        content_hash = sha256_file(local_pdf)
                        repo = repo or NCDRepository()
                        repo.upsert_pipeline_status(
                            s3_bucket=args.bucket,
                            s3_key=key,
                            s3_version_id=version_id,
                            content_hash=content_hash,
                            pipeline="tox",
                            status="failed",
                            error_message=pk_error,
                        )
                    except Exception:
                        pass
            finally:
                if local_pdf and local_pdf.exists():
                    try:
                        local_pdf.unlink()
                    except Exception:
                        pass
            pk_duration = (datetime.now(timezone.utc) - started).total_seconds()

        if summary_required and args.mode in {"auto", "core"}:
            started = datetime.now(timezone.utc)
            try:
                if args.summary_min_chars is not None:
                    os.environ["SECTION_SUMMARY_MIN_CHARS"] = str(
                        args.summary_min_chars
                    )
                if args.summary_max_chars is not None:
                    os.environ["SECTION_SUMMARY_MAX_CHARS"] = str(
                        args.summary_max_chars
                    )
                if args.summary_keywords_min is not None:
                    os.environ["SECTION_SUMMARY_KEYWORDS_MIN"] = str(
                        args.summary_keywords_min
                    )
                if args.summary_keywords_max is not None:
                    os.environ["SECTION_SUMMARY_KEYWORDS_MAX"] = str(
                        args.summary_keywords_max
                    )
                if args.summary_purpose is not None:
                    os.environ["SECTION_SUMMARY_PURPOSE"] = str(args.summary_purpose)
                if args.summary_type is not None:
                    os.environ["SECTION_SUMMARY_TYPE"] = str(args.summary_type)

                if not content_hash:
                    repo = repo or NCDRepository()
                    status_row = repo.fetch_ingestion_status_for_key(
                        s3_bucket=args.bucket,
                        s3_key=key,
                        s3_version_id=version_id,
                    )
                    if status_row and status_row.get("content_hash"):
                        content_hash = str(status_row.get("content_hash") or "")
                if not document_version_id:
                    repo = repo or NCDRepository()
                    if content_hash:
                        status_row = repo.fetch_ingestion_status(
                            s3_bucket=args.bucket,
                            s3_key=key,
                            content_hash=content_hash,
                        )
                        if status_row and status_row.get("document_version_id"):
                            document_version_id = str(
                                status_row.get("document_version_id")
                            )
                if not document_version_id:
                    raise RuntimeError(
                        "document_version_id is required for section summaries"
                    )
                if not content_hash:
                    raise RuntimeError("content_hash is required for section summaries")

                repo = repo or NCDRepository()
                repo.upsert_pipeline_status(
                    s3_bucket=args.bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    content_hash=content_hash,
                    pipeline="section-summary",
                    status="processing",
                    document_version_id=document_version_id,
                )

                markdown = _read_markdown_sidecar(s3_client, args.bucket, md_key)
                if not markdown:
                    raise RuntimeError("Markdown sidecar missing for section summaries")

                section_number, section_title = _derive_section_from_key(key)
                sections = [
                    SectionSpan(
                        section_number=section_number,
                        section_title=section_title,
                        char_start=0,
                        char_end=len(markdown),
                        page_start=None,
                        page_end=None,
                    )
                ]
                repo = repo or NCDRepository()
                persist_section_spans(
                    repo.session,
                    document_version_id,
                    sections,
                    replace_existing=True,
                )
                llm_client = _build_llm_client("real", args)
                summary_rows: List[Dict[str, Any]] = []
                for section in sections:
                    text = markdown[section.char_start : section.char_end].strip()
                    if not text:
                        summary_text = {
                            "summary": "No extractable text for this file.",
                            "topics": [],
                        }
                        keywords = section_summary._pad_keywords([], section)
                    else:
                        snippet = text
                        if args.summary_max_chars is not None:
                            snippet = snippet[: args.summary_max_chars]
                        payload = section_summary._summarize_section(
                            llm_client, section, snippet
                        )
                        if payload:
                            summary_text = payload.get("summary_text") or {}
                            keywords = payload.get("keywords") or []
                        else:
                            summary_text = {
                                "summary": "Summary generation failed.",
                                "topics": [],
                            }
                            keywords = section_summary._pad_keywords([], section)
                    summary_rows.append(
                        {
                            "section_number": section.section_number,
                            "section_title": section.section_title,
                            "summary_text": summary_text,
                            "keywords": keywords,
                        }
                    )

                stored = section_summary._persist_summaries(
                    document_version_id,
                    sections,
                    summary_rows,
                    model_name=llm_client.model_name,
                )
                summary_status = "completed"
                summary_count = len(summary_rows)
                repo.upsert_pipeline_status(
                    s3_bucket=args.bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    content_hash=content_hash,
                    pipeline="section-summary",
                    status="completed",
                    document_version_id=document_version_id,
                )
                if stored.startswith("failed"):
                    summary_status = "failed"
                    summary_error = stored
            except Exception as exc:
                summary_status = "failed"
                summary_error = _format_error(exc)
                try:
                    repo = repo or NCDRepository()
                    repo.upsert_pipeline_status(
                        s3_bucket=args.bucket,
                        s3_key=key,
                        s3_version_id=version_id,
                        content_hash=content_hash,
                        pipeline="section-summary",
                        status="failed",
                        document_version_id=document_version_id or None,
                        error_message=summary_error,
                    )
                except Exception:
                    pass
            summary_duration = (datetime.now(timezone.utc) - started).total_seconds()

        if pharm_overview_required and args.mode in {"auto", "core"}:
            started = datetime.now(timezone.utc)
            try:
                if not args.project_id:
                    raise RuntimeError(
                        "project_id is required for pharmacology overview extraction"
                    )
                if not module4_section:
                    raise RuntimeError("Unable to derive Module 4 section from key")
                if not content_hash:
                    repo = repo or NCDRepository()
                    status_row = repo.fetch_pipeline_status_for_key(
                        s3_bucket=args.bucket,
                        s3_key=key,
                        s3_version_id=version_id,
                        pipeline="core",
                    )
                    if status_row and status_row.get("content_hash"):
                        content_hash = str(status_row.get("content_hash") or "")
                if not content_hash:
                    raise RuntimeError("content_hash is required for pharm-overview")

                repo = repo or NCDRepository()
                repo.upsert_pipeline_status(
                    s3_bucket=args.bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    content_hash=content_hash,
                    pipeline="pharm-overview",
                    status="processing",
                    document_version_id=document_version_id or None,
                )

                markdown = _read_markdown_sidecar(s3_client, args.bucket, md_key)
                if not markdown:
                    raise RuntimeError("Markdown sidecar missing for pharm-overview")

                metadata = _extract_pharm_overview_metadata(
                    key=key,
                    file_name=file_name,
                    module4_section=module4_section,
                    markdown=markdown,
                    content_hash=content_hash,
                )
                (
                    pharm_overview_source_document_id,
                    pharm_overview_study_id,
                ) = _upsert_pharm_overview_to_db(
                    repo=repo,
                    project_id=args.project_id,
                    file_name=file_name,
                    content_hash=content_hash,
                    module4_section=module4_section,
                    metadata=metadata,
                )
                pharm_overview_study_number = str(
                    metadata.get("study_number") or ""
                ).strip()
                pharm_overview_module4_section = module4_section
                pharm_overview_status = "completed"
                repo.upsert_pipeline_status(
                    s3_bucket=args.bucket,
                    s3_key=key,
                    s3_version_id=version_id,
                    content_hash=content_hash,
                    pipeline="pharm-overview",
                    status="completed",
                    document_version_id=document_version_id or None,
                    source_document_id=pharm_overview_source_document_id or None,
                    study_id=pharm_overview_study_id or None,
                )
            except Exception as exc:
                pharm_overview_status = "failed"
                pharm_overview_error = _format_error(exc)
                try:
                    if content_hash:
                        repo = repo or NCDRepository()
                        repo.upsert_pipeline_status(
                            s3_bucket=args.bucket,
                            s3_key=key,
                            s3_version_id=version_id,
                            content_hash=content_hash,
                            pipeline="pharm-overview",
                            status="failed",
                            document_version_id=document_version_id or None,
                            error_message=pharm_overview_error,
                        )
                except Exception:
                    pass
            pharm_overview_duration = (
                datetime.now(timezone.utc) - started
            ).total_seconds()

        if not args.no_meta and args.mode in {"auto", "core"}:
            try:
                company, project = _parse_company_project(key)
                meta_payload = {
                    "bucket": args.bucket,
                    "key": key,
                    "version_id": version_id,
                    "document_version_id": document_version_id or None,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "company": company,
                    "project": project,
                    "markdown_key": md_key if md_exists else None,
                    "quality_key": f"{key}.quality.json",
                    "core_status": core_status,
                    "langchain_status": langchain_status,
                    "context_status": context_status,
                    "section_summary_status": summary_status,
                    "section_summary_count": summary_count,
                    "pharm_overview_status": pharm_overview_status,
                    "pharm_overview_study_number": pharm_overview_study_number or None,
                    "pharm_overview_module4_section": (
                        pharm_overview_module4_section or None
                    ),
                    "tox_status": tox_status,
                    "pk_status": pk_status,
                }
                meta_key = (
                    _upload_meta_json(s3_client, args.bucket, key, meta_payload) or ""
                )
            except Exception as exc:
                print(
                    f"[WARN] meta file upload failed for {key}: {exc}", file=sys.stderr
                )
        return {
            "key": key,
            "display_key": display_key,
            "version_id": version_id,
            "last_modified": last_modified,
            "md_exists": md_exists,
            "status_completed": status_completed,
            "tox_data_exists": tox_data_exists,
            "pk_data_exists": pk_data_exists,
            "core_required": core_required,
            "core_status": core_status,
            "core_error": core_error,
            "core_duration_seconds": round(core_duration, 3),
            "langchain_required": langchain_required,
            "langchain_status": langchain_status,
            "langchain_error": langchain_error,
            "langchain_duration_seconds": round(langchain_duration, 3),
            "context_required": context_required,
            "context_status": context_status,
            "context_error": context_error,
            "context_duration_seconds": round(context_duration, 3),
            "summary_required": summary_required,
            "summary_status": summary_status,
            "summary_error": summary_error,
            "summary_duration_seconds": round(summary_duration, 3),
            "summary_count": summary_count,
            "pharm_overview_required": pharm_overview_required,
            "pharm_overview_status": pharm_overview_status,
            "pharm_overview_error": pharm_overview_error,
            "pharm_overview_duration_seconds": round(pharm_overview_duration, 3),
            "pharm_overview_source_document_id": pharm_overview_source_document_id,
            "pharm_overview_study_id": pharm_overview_study_id,
            "pharm_overview_study_number": pharm_overview_study_number,
            "pharm_overview_module4_section": pharm_overview_module4_section,
            "document_version_id": document_version_id,
            "tox_required": tox_required,
            "tox_status": tox_status,
            "tox_error": tox_error,
            "tox_duration_seconds": round(tox_duration, 3),
            "tox_source_document_id": tox_source_document_id,
            "tox_study_id": tox_study_id,
            "pk_required": pk_required,
            "pk_status": pk_status,
            "pk_error": pk_error,
            "pk_duration_seconds": round(pk_duration, 3),
            "pk_source_document_id": pk_source_document_id,
            "pk_study_id": pk_study_id,
            "meta_key": meta_key,
        }
    except Exception as exc:
        core_error = core_error or _format_error(exc)
        return {
            "key": key,
            "display_key": display_key,
            "version_id": version_id,
            "last_modified": last_modified,
            "md_exists": md_exists,
            "status_completed": status_completed,
            "tox_data_exists": tox_data_exists,
            "pk_data_exists": pk_data_exists,
            "core_required": core_required,
            "core_status": "failed",
            "core_error": core_error,
            "core_duration_seconds": round(core_duration, 3),
            "langchain_required": langchain_required,
            "langchain_status": "failed" if langchain_required else langchain_status,
            "langchain_error": langchain_error,
            "langchain_duration_seconds": round(langchain_duration, 3),
            "context_required": context_required,
            "context_status": "failed" if context_required else context_status,
            "context_error": context_error,
            "context_duration_seconds": round(context_duration, 3),
            "summary_required": summary_required,
            "summary_status": "failed" if summary_required else summary_status,
            "summary_error": summary_error,
            "summary_duration_seconds": round(summary_duration, 3),
            "summary_count": summary_count,
            "pharm_overview_required": pharm_overview_required,
            "pharm_overview_status": (
                "failed" if pharm_overview_required else pharm_overview_status
            ),
            "pharm_overview_error": pharm_overview_error,
            "pharm_overview_duration_seconds": round(pharm_overview_duration, 3),
            "pharm_overview_source_document_id": pharm_overview_source_document_id,
            "pharm_overview_study_id": pharm_overview_study_id,
            "pharm_overview_study_number": pharm_overview_study_number,
            "pharm_overview_module4_section": pharm_overview_module4_section,
            "document_version_id": document_version_id,
            "tox_required": tox_required,
            "tox_status": "failed" if tox_required else tox_status,
            "tox_error": tox_error,
            "tox_duration_seconds": round(tox_duration, 3),
            "tox_source_document_id": tox_source_document_id,
            "tox_study_id": tox_study_id,
            "pk_required": pk_required,
            "pk_status": "failed" if pk_required else pk_status,
            "pk_error": pk_error,
            "pk_duration_seconds": round(pk_duration, 3),
            "pk_source_document_id": pk_source_document_id,
            "pk_study_id": pk_study_id,
            "meta_key": meta_key,
        }
    finally:
        if repo is not None:
            repo.close()


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    _configure_openai_limiters(args.llm_max_concurrent)

    if not args.bucket:
        print("S3 bucket is required (set --bucket or S3_BUCKET).", file=sys.stderr)
        return 1

    if not args.prefix and not args.project:
        print("Project is required unless --prefix is supplied.", file=sys.stderr)
        return 1

    if args.mode in {"auto", "core"}:
        if not args.tenant_id:
            print(
                "tenant_id is required (set --tenant-id or DEFAULT_TENANT_ID).",
                file=sys.stderr,
            )
            return 1
        if not args.created_by:
            print(
                "created_by is required (set --created-by or DEFAULT_USER_ID).",
                file=sys.stderr,
            )
            return 1

    if args.mode in {"auto", "tox", "pk"} and not args.project_id:
        if not args.project:
            print(
                "project is required to auto-create project_id for tox/pk pipeline.",
                file=sys.stderr,
            )
            return 1
        try:
            repo = NCDRepository()
            args.project_id = _ensure_project_id(
                repo,
                project_name=args.project,
                tenant_id=args.tenant_id,
                created_by=args.created_by,
                product_type=args.project_product_type,
                sponsor_email=args.project_sponsor_email,
                sponsor_name=args.project_sponsor_name,
            )
        except Exception as exc:
            print(f"Unable to ensure project_id: {exc}", file=sys.stderr)
            return 1
        finally:
            try:
                repo.close()
            except Exception:
                pass

    prefix = args.prefix or f"{args.company.rstrip('/')}/{args.project.strip('/')}/"
    if prefix.lower().endswith(".pdf"):
        prefix = prefix
    elif not prefix.endswith("/"):
        prefix = f"{prefix}/"
    args.run_prefix = prefix
    args.project_root_prefix = _project_root_prefix(args, prefix)
    print(
        "[INFO] Logging file paths relative to "
        f"'{args.project_root_prefix.strip('/')}/'"
    )

    if args.core_text_engines:
        os.environ["PDF_PIPELINE_TEXT_ENGINES"] = args.core_text_engines
    if args.disable_core_tables:
        os.environ["PDF_PIPELINE_DISABLE_TABLES"] = "1"
    elif args.core_table_engines:
        os.environ["PDF_PIPELINE_TABLE_ENGINES"] = args.core_table_engines
    else:
        # Safe default to avoid camelot/tabula crashes; override with --core-table-engines.
        os.environ.setdefault("PDF_PIPELINE_TABLE_ENGINES", "pdfplumber")

    client = _require_boto3().client("s3", region_name=args.aws_region)

    run_id = f"module{args.module}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    report_dir = Path(args.report_dir).expanduser()
    report_dir.mkdir(parents=True, exist_ok=True)
    report_json_path = report_dir / f"ingestion_{run_id}.json"
    report_csv_path = report_dir / f"ingestion_{run_id}.csv"

    started_at = datetime.now(timezone.utc)
    entries: list[Dict[str, Any]] = []
    doc_processed = 0
    doc_failed = 0
    doc_skipped = 0
    core_runs = 0
    core_failed = 0
    langchain_runs = 0
    langchain_failed = 0
    context_runs = 0
    context_failed = 0
    summary_runs = 0
    summary_failed = 0
    pharm_overview_runs = 0
    pharm_overview_failed = 0
    tox_runs = 0
    tox_failed = 0
    pk_runs = 0
    pk_failed = 0
    error_counts: Dict[str, int] = {}

    docs = _collect_candidate_docs(client, args, prefix)
    if not docs:
        decoded_prefix = unquote_plus(prefix)
        if decoded_prefix != prefix:
            if (
                not decoded_prefix.lower().endswith(".pdf")
                and not decoded_prefix.endswith("/")
            ):
                decoded_prefix = f"{decoded_prefix}/"
            decoded_docs = _collect_candidate_docs(client, args, decoded_prefix)
            if decoded_docs:
                print(
                    "[WARN] No files found with the provided prefix. "
                    f"Using URL-decoded prefix '{decoded_prefix}' instead."
                )
                prefix = decoded_prefix
                args.run_prefix = prefix
                args.project_root_prefix = _project_root_prefix(args, prefix)
                docs = decoded_docs

    print(f"[INFO] Queued {len(docs)} PDF file(s) from prefix '{prefix}'")
    if not docs:
        print(
            "[WARN] No matching PDFs found. Check project/prefix spelling "
            "(for example, ASONEP2 vs ASPONEP2) and avoid URL-encoded '+' in paths."
        )

    pending_docs = docs
    if not args.dry_run and docs:
        pending_docs = []
        guard_skipped = 0
        guard_repo: Optional[NCDRepository] = None
        try:
            guard_repo = NCDRepository()
            for obj in docs:
                key = str(obj.get("key") or "")
                display_key = _display_key_for_logs(key, args)
                try:
                    fully_ingested, precheck_entry = _is_fully_ingested(
                        obj,
                        args=args,
                        s3_client=client,
                        repo=guard_repo,
                    )
                except Exception as exc:
                    print(
                        f"[WARN] Guard check failed for {display_key}: {exc}",
                        file=sys.stderr,
                    )
                    pending_docs.append(obj)
                    continue

                if fully_ingested:
                    entries.append(precheck_entry)
                    guard_skipped += 1
                    doc_skipped += 1
                    print(f"[ALREADY_INGESTED] {display_key}")
                else:
                    pending_docs.append(obj)
        finally:
            if guard_repo is not None:
                guard_repo.close()

        print(
            f"[INFO] Pending {len(pending_docs)} file(s); "
            f"already ingested {guard_skipped} file(s)"
        )

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(_process_document, obj, args) for obj in pending_docs]
        for future in as_completed(futures):
            entry = future.result()
            entries.append(entry)

            required = (
                entry.get("core_required")
                or entry.get("langchain_required")
                or entry.get("context_required")
                or entry.get("summary_required")
                or entry.get("pharm_overview_required")
                or entry.get("tox_required")
                or entry.get("pk_required")
            )
            failed = (
                (entry.get("core_required") and entry.get("core_status") == "failed")
                or (
                    entry.get("langchain_required")
                    and entry.get("langchain_status") == "failed"
                )
                or (
                    entry.get("context_required")
                    and entry.get("context_status") == "failed"
                )
                or (
                    entry.get("summary_required")
                    and entry.get("summary_status") == "failed"
                )
                or (
                    entry.get("pharm_overview_required")
                    and entry.get("pharm_overview_status") == "failed"
                )
                or (entry.get("tox_required") and entry.get("tox_status") == "failed")
                or (entry.get("pk_required") and entry.get("pk_status") == "failed")
            )

            if entry.get("core_required"):
                core_runs += 1
                if entry.get("core_status") == "failed":
                    core_failed += 1
            if entry.get("langchain_required"):
                langchain_runs += 1
                if entry.get("langchain_status") == "failed":
                    langchain_failed += 1
            if entry.get("context_required"):
                context_runs += 1
                if entry.get("context_status") == "failed":
                    context_failed += 1
            if entry.get("summary_required"):
                summary_runs += 1
                if entry.get("summary_status") == "failed":
                    summary_failed += 1
            if entry.get("pharm_overview_required"):
                pharm_overview_runs += 1
                if entry.get("pharm_overview_status") == "failed":
                    pharm_overview_failed += 1
            if entry.get("tox_required"):
                tox_runs += 1
                if entry.get("tox_status") == "failed":
                    tox_failed += 1
            if entry.get("pk_required"):
                pk_runs += 1
                if entry.get("pk_status") == "failed":
                    pk_failed += 1

            if failed:
                doc_failed += 1
            elif required:
                doc_processed += 1
            else:
                doc_skipped += 1

            for err in (
                entry.get("core_error"),
                entry.get("langchain_error"),
                entry.get("context_error"),
                entry.get("summary_error"),
                entry.get("pharm_overview_error"),
                entry.get("tox_error"),
                entry.get("pk_error"),
            ):
                if err:
                    name = err.split(":", 1)[0]
                    error_counts[name] = error_counts.get(name, 0) + 1

            status = "skipped"
            if failed:
                status = "failed"
            elif required:
                status = "completed"
            print(f"[{status.upper()}] {entry.get('display_key') or entry['key']}")
            if failed:
                if entry.get("core_error"):
                    print(f"  core_error: {entry['core_error']}", file=sys.stderr)
                if entry.get("langchain_error"):
                    print(
                        f"  langchain_error: {entry['langchain_error']}",
                        file=sys.stderr,
                    )
                if entry.get("context_error"):
                    print(f"  context_error: {entry['context_error']}", file=sys.stderr)
                if entry.get("summary_error"):
                    print(f"  summary_error: {entry['summary_error']}", file=sys.stderr)
                if entry.get("pharm_overview_error"):
                    print(
                        f"  pharm_overview_error: {entry['pharm_overview_error']}",
                        file=sys.stderr,
                    )
                if entry.get("tox_error"):
                    print(f"  tox_error: {entry['tox_error']}", file=sys.stderr)
                if entry.get("pk_error"):
                    print(f"  pk_error: {entry['pk_error']}", file=sys.stderr)

    ended_at = datetime.now(timezone.utc)
    report = {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "bucket": args.bucket,
        "company": args.company,
        "project": args.project,
        "project_id": args.project_id,
        "module": args.module,
        "all_modules": args.all_modules,
        "prefix": prefix,
        "mode": args.mode,
        "dry_run": args.dry_run,
        "core_check": args.core_check,
        "md_suffix": args.md_suffix,
        "force_core": bool(args.force or args.force_core),
        "force_langchain": args.force_langchain,
        "force_pharm_overview": args.force_pharm_overview,
        "force_tox": args.force_tox,
        "force_pk": args.force_pk,
        "workers": args.workers,
        "llm_max_concurrent": args.llm_max_concurrent,
        "llm_min_interval_seconds": args.llm_min_interval_seconds,
        "llm_max_retries": args.llm_max_retries,
        "llm_initial_backoff_seconds": args.llm_initial_backoff_seconds,
        "llm_max_backoff_seconds": args.llm_max_backoff_seconds,
        "llm_max_retry_after_seconds": args.llm_max_retry_after_seconds,
        "llm_max_total_retry_seconds": args.llm_max_total_retry_seconds,
        "llm_jitter_seconds": args.llm_jitter_seconds,
        "llm_fallback_models": _parse_csv_models(args.llm_fallback_models),
        "doc_processed": doc_processed,
        "doc_failed": doc_failed,
        "doc_skipped": doc_skipped,
        "core_runs": core_runs,
        "core_failed": core_failed,
        "langchain_runs": langchain_runs,
        "langchain_failed": langchain_failed,
        "context_runs": context_runs,
        "context_failed": context_failed,
        "summary_runs": summary_runs,
        "summary_failed": summary_failed,
        "pharm_overview_runs": pharm_overview_runs,
        "pharm_overview_failed": pharm_overview_failed,
        "tox_runs": tox_runs,
        "tox_failed": tox_failed,
        "pk_runs": pk_runs,
        "pk_failed": pk_failed,
        "error_summary": error_counts,
        "entries": entries,
    }

    with report_json_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    with report_csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "key",
                "display_key",
                "version_id",
                "last_modified",
                "md_exists",
                "status_completed",
                "tox_data_exists",
                "pk_data_exists",
                "core_required",
                "core_status",
                "core_error",
                "core_duration_seconds",
                "langchain_required",
                "langchain_status",
                "langchain_error",
                "langchain_duration_seconds",
                "context_required",
                "context_status",
                "context_error",
                "context_duration_seconds",
                "summary_required",
                "summary_status",
                "summary_error",
                "summary_duration_seconds",
                "summary_count",
                "pharm_overview_required",
                "pharm_overview_status",
                "pharm_overview_error",
                "pharm_overview_duration_seconds",
                "pharm_overview_source_document_id",
                "pharm_overview_study_id",
                "pharm_overview_study_number",
                "pharm_overview_module4_section",
                "document_version_id",
                "tox_required",
                "tox_status",
                "tox_error",
                "tox_duration_seconds",
                "tox_source_document_id",
                "tox_study_id",
                "pk_required",
                "pk_status",
                "pk_error",
                "pk_duration_seconds",
                "pk_source_document_id",
                "pk_study_id",
                "meta_key",
            ],
        )
        writer.writeheader()
        writer.writerows(entries)

    try:
        repo = NCDRepository()
        repo.create_ingestion_run_report(
            run_id=run_id,
            bucket=args.bucket,
            company=args.company,
            project=args.project,
            project_id=args.project_id,
            module=args.module,
            mode=args.mode,
            core_check=args.core_check,
            md_suffix=args.md_suffix,
            force_core=bool(args.force or args.force_core),
            force_tox=args.force_tox,
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            doc_processed=doc_processed,
            doc_failed=doc_failed,
            doc_skipped=doc_skipped,
            core_runs=core_runs,
            core_failed=core_failed,
            tox_runs=tox_runs,
            tox_failed=tox_failed,
            error_summary=error_counts,
            report_json=report,
        )
    except Exception as exc:
        print(f"[WARN] Unable to persist run report to DB: {exc}", file=sys.stderr)

    if (
        not args.dry_run
        and args.mode in {"auto", "core"}
        and args.project_id
        and args.tenant_id
        and (summary_runs > 0 or context_runs > 0)
    ):
        try:
            repo = NCDRepository()
            db = repo.session
            for element in list_template_elements():
                payload = build_ctd_element_reference(
                    db,
                    tenant_id=args.tenant_id,
                    project_id=args.project_id,
                    bucket=args.bucket,
                    element_number=element,
                )
                repo.upsert_ctd_section_reference(
                    record=CTDSectionReferenceRecord(
                        tenant_id=args.tenant_id,
                        project_id=args.project_id,
                        bucket=args.bucket,
                        element_number=element,
                        section_number=payload.get("section_number"),
                        template_payload=payload.get("template") or {},
                        module4_sections=payload.get("module4_sections") or [],
                        payload=payload,
                    )
                )
        except Exception as exc:
            print(
                f"[WARN] Unable to refresh 2.4 element references: {exc}",
                file=sys.stderr,
            )
        finally:
            try:
                repo.close()
            except Exception:
                pass

    print(
        "\nCompleted. processed=%d failed=%d skipped=%d core_runs=%d langchain_runs=%d context_runs=%d summary_runs=%d pharm_overview_runs=%d tox_runs=%d pk_runs=%d"
        % (
            doc_processed,
            doc_failed,
            doc_skipped,
            core_runs,
            langchain_runs,
            context_runs,
            summary_runs,
            pharm_overview_runs,
            tox_runs,
            pk_runs,
        )
    )
    print(f"Report JSON: {report_json_path}")
    print(f"Report CSV:  {report_csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
