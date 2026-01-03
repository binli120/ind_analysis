# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session


_MAPPING_CACHE: List[Dict[str, Any]] | None = None


def _mapping_paths() -> List[Path]:
    base = Path(__file__).resolve().parents[1]
    return [
        base / "mapping" / "module4_to_26_mapping_complete.json",
        base.parent / "summary" / "module4_to_26_mapping_complete.json",
    ]


_SECTION_TOKEN_RE = re.compile(r"(?P<token>\d+(?:\.\d+)+(?:\|\d+(?:\.\d+)+)*)")
_MARKDOWN_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$")
_MARKDOWN_TABLE_RULE_RE = re.compile(r"^\s*\|(?:\s*:?-+:?\s*\|)+\s*$")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*]\(([^)]+)\)")


def load_module4_to_26_mapping() -> List[Dict[str, Any]]:
    global _MAPPING_CACHE
    if _MAPPING_CACHE is not None:
        return _MAPPING_CACHE

    raw = None
    attempted = []
    for path in _mapping_paths():
        attempted.append(str(path))
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        break
    if raw is None:
        raise FileNotFoundError(
            "Module 4 mapping file not found. Checked: " + ", ".join(attempted)
        )
    mappings: List[Dict[str, Any]] = []
    for group_key in (
        "pharmacology_mappings",
        "pharmacokinetics_mappings",
        "toxicology_mappings",
    ):
        group = raw.get(group_key, {})
        if not isinstance(group, dict):
            continue
        for module4_section, entry in group.items():
            if not isinstance(entry, dict):
                continue
            target_sections = entry.get("target_sections", {}) or {}
            targets: List[Dict[str, str]] = []
            for kind in ("written", "tabulated"):
                for section in target_sections.get(kind, []) or []:
                    targets.append({"section": str(section), "kind": kind})

            mappings.append(
                {
                    "module4_section": module4_section,
                    "category": entry.get("category"),
                    "priority": entry.get("priority"),
                    "target_sections": target_sections,
                    "required_content": entry.get("required_content", []),
                    "extraction_focus": entry.get("extraction_focus", {}),
                    "validation_rules": entry.get("validation_rules", {}),
                    "subsection_mapping": entry.get("subsection_mapping", {}),
                    "targets": targets,
                }
            )

    _MAPPING_CACHE = mappings
    return _MAPPING_CACHE


def module4_sections_for_ctd(ctd_section: str) -> List[Dict[str, Any]]:
    target = ctd_section.strip()
    if not target:
        return []

    mappings = load_module4_to_26_mapping()
    matched: List[Dict[str, Any]] = []
    for entry in mappings:
        for target_entry in entry.get("targets", []):
            section = target_entry.get("section", "")
            if section == target or section.startswith(f"{target}."):
                matched.append(entry)
                break
    return matched


_CTD_24_TO_26_PREFIXES = {
    "2.4.1": ["2.6"],
    "2.4.2": ["2.6.2"],
    "2.4.3": ["2.6.4"],
    "2.4.4": ["2.6.6"],
    "2.4.5": ["2.6.6", "2.6.7"],
    "2.4.6": ["2.6.7"],
}


def resolve_ctd_targets(ctd_section: str) -> List[str]:
    section = (ctd_section or "").strip()
    if not section:
        return []
    if section.startswith("2.6"):
        return [section]
    if section.startswith("2.4"):
        for prefix, targets in _CTD_24_TO_26_PREFIXES.items():
            if section.startswith(prefix):
                return list(targets)
        return ["2.6"]
    return []


def module4_sections_for_ctd_targets(
    ctd_section: str,
) -> tuple[List[str], List[Dict[str, Any]], List[str]]:
    targets = resolve_ctd_targets(ctd_section)
    matched_entries: List[Dict[str, Any]] = []
    seen = set()
    for target in targets:
        for entry in module4_sections_for_ctd(target):
            module4_section = entry.get("module4_section")
            if not module4_section or module4_section in seen:
                continue
            seen.add(module4_section)
            matched_entries.append(entry)
    return sorted(seen), matched_entries, targets


def extract_module4_token(section_number: str) -> str:
    match = _SECTION_TOKEN_RE.match(section_number or "")
    if not match:
        return ""
    return match.group("token")


def section_number_matches(section_number: str, module4_section: str) -> bool:
    if not section_number or not module4_section:
        return False
    if section_number == module4_section:
        return True
    if section_number.startswith(f"{module4_section}."):
        return True
    if f"|{module4_section}." in section_number:
        return True
    return False


def markdown_slice(markdown: str, char_start: int, char_end: int) -> str:
    start = max(char_start, 0)
    end = min(char_end, len(markdown))
    if end <= start:
        return ""
    return markdown[start:end]


def extract_markdown_tables(markdown: str) -> List[str]:
    tables: List[str] = []
    buffer: List[str] = []
    for line in markdown.splitlines():
        if _MARKDOWN_TABLE_ROW_RE.match(line):
            buffer.append(line)
        elif buffer:
            tables.append(_markdown_table_to_html(buffer))
            buffer = []
    if buffer:
        tables.append(_markdown_table_to_html(buffer))
    return [table for table in tables if table]


def _markdown_table_to_html(lines: List[str]) -> str:
    rows = [_split_markdown_row(line) for line in lines]
    if len(rows) < 2:
        return ""
    if _MARKDOWN_TABLE_RULE_RE.match(lines[1]):
        header = rows[0]
        body_rows = rows[2:]
    else:
        header = rows[0]
        body_rows = rows[1:]

    head_cells = "".join(f"<th>{_escape_html(cell)}</th>" for cell in header)
    body_html = []
    for row in body_rows:
        cells = "".join(f"<td>{_escape_html(cell)}</td>" for cell in row)
        body_html.append(f"<tr>{cells}</tr>")

    body = "".join(body_html) if body_html else ""
    return f"<table><thead><tr>{head_cells}</tr></thead><tbody>{body}</tbody></table>"


def _split_markdown_row(line: str) -> List[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def extract_markdown_images(markdown: str, bucket: str, base_key: str) -> List[str]:
    images: List[str] = []
    prefix = base_key.rsplit("/", 1)[0] if "/" in base_key else ""
    for match in _MARKDOWN_IMAGE_RE.finditer(markdown):
        uri = match.group(1).strip()
        if not uri:
            continue
        if (
            uri.startswith("s3://")
            or uri.startswith("http://")
            or uri.startswith("https://")
        ):
            images.append(uri)
        else:
            key = f"{prefix}/{uri}" if prefix else uri
            images.append(f"s3://{bucket}/{key}")
    return images


def fetch_project_name(db: Session, project_id: str) -> Optional[str]:
    row = (
        db.execute(
            sqltext(
                "SELECT to_jsonb(p) AS payload FROM projects p WHERE id = :pid LIMIT 1"
            ),
            {"pid": project_id},
        )
        .mappings()
        .first()
    )
    if not row or not row.get("payload"):
        return None
    payload = row["payload"] or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    return (
        payload.get("ind_title")
        or payload.get("drug_name")
        or payload.get("name")
        or payload.get("project_name")
    )


def fetch_section_sources(
    db: Session,
    *,
    tenant_id: str,
    bucket: str,
    project_like: str,
    module4_sections: List[str],
) -> List[Dict[str, Any]]:
    if not module4_sections:
        return []

    filters: List[str] = []
    params: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "bucket": bucket,
        "project_like": project_like,
    }
    for idx, module4_section in enumerate(module4_sections):
        key = f"mod_{idx}"
        params[key] = module4_section
        params[f"{key}_dot"] = f"{module4_section}.%"
        params[f"{key}_pipe"] = f"%|{module4_section}.%"
        filters.append(
            f"(ds.section_number = :{key} OR ds.section_number LIKE :{key}_dot "
            f"OR ds.section_number LIKE :{key}_pipe)"
        )

    filter_sql = " OR ".join(filters)

    rows = (
        db.execute(
            sqltext(
                f"""
                SELECT
                    ds.id AS section_id,
                    ds.section_number,
                    ds.section_title,
                    ds.char_start,
                    ds.char_end,
                    ds.page_start,
                    ds.page_end,
                    d.id AS document_id,
                    dv.id AS document_version_id,
                    dv.s3_bucket,
                    dv.s3_key,
                    dv.s3_version_id,
                    s.summary_text,
                    s.keywords,
                    s.summary_type,
                    s.summary_purpose
                FROM document_sections ds
                JOIN document_versions dv ON dv.id = ds.document_version_id
                JOIN documents d ON d.id = dv.document_id
                LEFT JOIN document_section_summary s
                  ON s.section_id = ds.id
                 AND s.summary_purpose = 'ctd_2_6'
                WHERE d.tenant_id = :tenant_id
                  AND dv.s3_bucket = :bucket
                  AND dv.s3_key ILIKE :project_like
                  AND ({filter_sql})
                ORDER BY dv.s3_key, ds.section_number
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def fetch_assets_for_sections(
    db: Session,
    *,
    tenant_id: str,
    bucket: str,
    project_like: str,
    module4_sections: List[str],
    asset_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not module4_sections:
        return []
    filters: List[str] = []
    params: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "bucket": bucket,
        "project_like": project_like,
    }
    if asset_type:
        params["asset_type"] = asset_type
    for idx, module4_section in enumerate(module4_sections):
        key = f"mod_{idx}"
        params[key] = module4_section
        params[f"{key}_dot"] = f"{module4_section}.%"
        params[f"{key}_pipe"] = f"%|{module4_section}.%"
        filters.append(
            f"(ds.section_number = :{key} OR ds.section_number LIKE :{key}_dot "
            f"OR ds.section_number LIKE :{key}_pipe)"
        )
    filter_sql = " OR ".join(filters)

    rows = (
        db.execute(
            sqltext(
                f"""
                SELECT DISTINCT
                    da.id,
                    da.asset_type,
                    da.page_number,
                    da.index_on_page,
                    da.s3_bucket,
                    da.s3_key,
                    da.caption,
                    da.description,
                    da.keywords,
                    da.extra_attributes,
                    dv.id AS document_version_id,
                    dv.s3_key AS document_s3_key
                FROM document_assets da
                JOIN document_versions dv ON dv.id = da.document_version_id
                JOIN documents d ON d.id = dv.document_id
                JOIN document_sections ds ON ds.document_version_id = dv.id
                WHERE d.tenant_id = :tenant_id
                  AND dv.s3_bucket = :bucket
                  AND dv.s3_key ILIKE :project_like
                  AND ({filter_sql})
                  {"AND da.asset_type = :asset_type" if asset_type else ""}
                ORDER BY dv.s3_key, da.page_number, da.index_on_page
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def fetch_key_sections_for_sections(
    db: Session,
    *,
    tenant_id: str,
    bucket: str,
    project_like: str,
    module4_sections: List[str],
    section_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not module4_sections:
        return []
    filters: List[str] = []
    params: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "bucket": bucket,
        "project_like": project_like,
    }
    if section_type:
        params["section_type"] = section_type
    for idx, module4_section in enumerate(module4_sections):
        key = f"mod_{idx}"
        params[key] = module4_section
        params[f"{key}_dot"] = f"{module4_section}.%"
        params[f"{key}_pipe"] = f"%|{module4_section}.%"
        filters.append(
            f"(ds.section_number = :{key} OR ds.section_number LIKE :{key}_dot "
            f"OR ds.section_number LIKE :{key}_pipe)"
        )
    filter_sql = " OR ".join(filters)

    rows = (
        db.execute(
            sqltext(
                f"""
                SELECT DISTINCT
                    ks.id,
                    ks.section_type,
                    ks.text,
                    ks.page_start,
                    ks.page_end,
                    ks.char_start,
                    ks.char_end,
                    ks.asset_ids,
                    ks.model_name,
                    ks.confidence,
                    dv.id AS document_version_id,
                    dv.s3_bucket,
                    dv.s3_key AS document_s3_key
                FROM document_key_sections ks
                JOIN document_versions dv ON dv.id = ks.document_version_id
                JOIN documents d ON d.id = dv.document_id
                JOIN document_sections ds ON ds.document_version_id = dv.id
                WHERE d.tenant_id = :tenant_id
                  AND dv.s3_bucket = :bucket
                  AND dv.s3_key ILIKE :project_like
                  AND ({filter_sql})
                  {"AND ks.section_type = :section_type" if section_type else ""}
                ORDER BY dv.s3_key, ks.page_start, ks.section_type
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def fetch_ncd_payload(
    db: Session,
    *,
    project_id: str,
    module4_sections: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    payload = {
        "studies": [],
        "dose_groups": [],
        "exposure_metrics": [],
        "findings": [],
        "safety_summaries": [],
        "source_documents": [],
    }
    if not module4_sections:
        return payload

    studies = (
        db.execute(
            sqltext(
                """
                SELECT * FROM ncd_study
                WHERE project_id = :pid
                  AND module4_section = ANY(:sections)
                """
            ),
            {"pid": project_id, "sections": module4_sections},
        )
        .mappings()
        .all()
    )
    payload["studies"] = [dict(row) for row in studies]
    study_ids = [row["id"] for row in studies if row.get("id")]
    if not study_ids:
        return payload

    payload["dose_groups"] = [
        dict(row)
        for row in db.execute(
            sqltext("SELECT * FROM ncd_dose_group WHERE study_id = ANY(:ids)"),
            {"ids": study_ids},
        )
        .mappings()
        .all()
    ]
    payload["exposure_metrics"] = [
        dict(row)
        for row in db.execute(
            sqltext("SELECT * FROM ncd_exposure_metric WHERE study_id = ANY(:ids)"),
            {"ids": study_ids},
        )
        .mappings()
        .all()
    ]
    payload["findings"] = [
        dict(row)
        for row in db.execute(
            sqltext("SELECT * FROM ncd_finding WHERE study_id = ANY(:ids)"),
            {"ids": study_ids},
        )
        .mappings()
        .all()
    ]
    payload["safety_summaries"] = [
        dict(row)
        for row in db.execute(
            sqltext(
                "SELECT * FROM ncd_study_safety_summary WHERE study_id = ANY(:ids)"
            ),
            {"ids": study_ids},
        )
        .mappings()
        .all()
    ]
    payload["source_documents"] = [
        dict(row)
        for row in db.execute(
            sqltext("SELECT * FROM ncd_source_document WHERE project_id = :pid"),
            {"pid": project_id},
        )
        .mappings()
        .all()
    ]
    return payload
