"""Gap-analysis helpers for Module 4 evidence coverage."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Set

from rapidfuzz import fuzz
from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

from ncd.types.ctd_materials import (
    fetch_project_name,
    load_module4_to_26_mapping,
    section_number_matches,
)
from pdf_analysis.api.s3_utils import _boto3_client

logger = logging.getLogger(__name__)


_GAP_FIELD_STOPWORDS: Set[str] = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "if",
    "in",
    "into",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}

_GAP_MODULE_RE = re.compile(r"\bmodule[\s._-]*([1-5])(?!\d)", re.IGNORECASE)
_GAP_MODULES = ("1", "2", "3", "4", "5")


def _gap_normalize_project_prefix(prefix: Optional[str]) -> str:
    """Gap normalize project prefix."""
    if not prefix:
        return ""
    cleaned = prefix.strip().lstrip("/")
    if cleaned and not cleaned.endswith("/"):
        cleaned = f"{cleaned}/"
    return cleaned


def _gap_collect_required_fields(entry: Dict[str, Any]) -> List[str]:
    """Gap collect required fields."""
    values: List[str] = []

    def _add(value: Any) -> None:
        """Add."""
        if not value:
            return
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            return
        text = str(value).strip()
        if not text:
            return
        if not re.search(r"[A-Za-z]", text):
            return
        values.append(text)

    def _visit(value: Any) -> None:
        """Visit."""
        if isinstance(value, dict):
            for key, item in value.items():
                _add(str(key).replace("_", " "))
                _visit(item)
            return
        if isinstance(value, list):
            for item in value:
                _visit(item)
            return
        _add(value)

    for field in ("required_content", "required_parameters"):
        field_values = entry.get(field)
        if isinstance(field_values, list):
            for item in field_values:
                _add(item)
    _visit(entry.get("validation_rules") or {})
    _visit(entry.get("extraction_focus") or {})

    deduped: List[str] = []
    seen: Set[str] = set()
    for item in values:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _gap_collect_required_sections(include_optional_p1: bool) -> List[Dict[str, Any]]:
    """Gap collect required sections."""
    rows: List[Dict[str, Any]] = []
    for entry in load_module4_to_26_mapping():
        module4_section = str(entry.get("module4_section") or "").strip()
        if not module4_section:
            continue
        priority = str(entry.get("priority") or "").strip().upper() or "P1"
        if priority == "P2":
            continue
        if priority == "P1" and not include_optional_p1:
            continue
        rows.append(
            {
                "module4_section": module4_section,
                "priority": priority,
                "category": entry.get("category"),
                "required_fields": _gap_collect_required_fields(entry),
                "mapping": entry,
            }
        )
    rows.sort(key=lambda row: row["module4_section"])
    return rows


def _gap_key_mentions_module4_section(key: str, module4_section: str) -> bool:
    """Gap key mentions module4 section."""
    key_tokens = re.findall(r"\d+(?:\.\d+)+|\d{4,}", key or "")
    target = module4_section.strip()
    if not target:
        return False
    collapsed_target = target.replace(".", "")
    for token in key_tokens:
        if token == target or token.startswith(f"{target}."):
            return True
        if token.isdigit() and (
            token == collapsed_target or token.startswith(collapsed_target)
        ):
            return True
    return False


def _gap_section_matches(value: Optional[str], module4_section: str) -> bool:
    """Gap section matches."""
    section = (value or "").strip()
    if not section:
        return False
    return section_number_matches(section, module4_section)


def _gap_stringify(value: Any) -> str:
    """Gap stringify."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=True, default=str)
    except Exception:
        return str(value)


def _gap_tokenize(value: str) -> List[str]:
    """Gap tokenize."""
    return [token for token in re.findall(r"[a-z0-9]+", value.lower()) if token]


def _gap_field_is_present(field: str, corpus: str) -> bool:
    """Gap field is present."""
    field_text = field.strip().lower()
    corpus_text = (corpus or "").lower()
    if not field_text or not corpus_text:
        return False
    if field_text in corpus_text:
        return True
    if fuzz.partial_ratio(field_text, corpus_text) >= 88:
        return True
    tokens = [
        token
        for token in _gap_tokenize(field_text)
        if token not in _GAP_FIELD_STOPWORDS and len(token) > 1
    ]
    if not tokens:
        return False
    hits = sum(1 for token in tokens if token in corpus_text)
    threshold = max(1, int(len(tokens) * 0.6))
    return hits >= threshold


def _gap_parse_module_number(value: Optional[str]) -> Optional[str]:
    """Gap parse module number."""
    text = (value or "").strip()
    if not text:
        return None
    if text in _GAP_MODULES:
        return text
    match = _GAP_MODULE_RE.search(text)
    if match:
        return match.group(1)
    return None


def _gap_project_filter_sql(
    *,
    project_name: Optional[str],
    project_prefix: Optional[str],
    params: Dict[str, Any],
) -> str:
    """Gap project filter sql."""
    normalized_prefix = _gap_normalize_project_prefix(project_prefix)
    if normalized_prefix:
        params["project_prefix_like"] = f"{normalized_prefix}%"
        return "AND dv.s3_key ILIKE :project_prefix_like"
    if project_name:
        params["project_name_like"] = f"%/{project_name}/%"
        return "AND dv.s3_key ILIKE :project_name_like"
    return ""


def _gap_section_filter_sql(
    module4_sections: Sequence[str], params: Dict[str, Any]
) -> str:
    """Gap section filter sql."""
    if not module4_sections:
        return ""
    predicates: List[str] = []
    for idx, section in enumerate(module4_sections):
        key = f"gap_mod_{idx}"
        params[key] = section
        params[f"{key}_dot"] = f"{section}.%"
        params[f"{key}_pipe"] = f"%|{section}.%"
        predicates.append(
            f"(ds.section_number = :{key} OR ds.section_number LIKE :{key}_dot "
            f"OR ds.section_number LIKE :{key}_pipe)"
        )
    return "AND (" + " OR ".join(predicates) + ")"


def _gap_fetch_project_document_rows(
    db: Session,
    *,
    bucket: str,
    tenant_id: Optional[str],
    project_name: Optional[str],
    project_prefix: Optional[str],
) -> List[Dict[str, Any]]:
    """Gap fetch project document rows."""
    params: Dict[str, Any] = {"bucket": bucket, "tenant_id": tenant_id}
    project_filter = _gap_project_filter_sql(
        project_name=project_name,
        project_prefix=project_prefix,
        params=params,
    )
    rows = (
        db.execute(
            sqltext(
                f"""
                SELECT DISTINCT
                    dv.id AS document_version_id,
                    d.id AS document_id,
                    d.tenant_id::text AS tenant_id,
                    dv.s3_bucket,
                    dv.s3_key,
                    dv.created_at
                FROM document_versions dv
                JOIN documents d ON d.id = dv.document_id
                WHERE dv.s3_bucket = :bucket
                  AND (:tenant_id IS NULL OR d.tenant_id::text = :tenant_id)
                  {project_filter}
                ORDER BY dv.s3_key
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _gap_fetch_section_rows(
    db: Session,
    *,
    bucket: str,
    tenant_id: Optional[str],
    project_name: Optional[str],
    project_prefix: Optional[str],
    module4_sections: Sequence[str],
) -> List[Dict[str, Any]]:
    """Gap fetch section rows."""
    params: Dict[str, Any] = {"bucket": bucket, "tenant_id": tenant_id}
    project_filter = _gap_project_filter_sql(
        project_name=project_name,
        project_prefix=project_prefix,
        params=params,
    )
    section_filter = _gap_section_filter_sql(module4_sections, params)
    rows = (
        db.execute(
            sqltext(
                f"""
                SELECT
                    ds.id AS section_id,
                    ds.section_number,
                    ds.section_title,
                    ds.page_start,
                    ds.page_end,
                    ds.char_start,
                    ds.char_end,
                    d.tenant_id::text AS tenant_id,
                    dv.id AS document_version_id,
                    dv.s3_bucket,
                    dv.s3_key,
                    s.summary_text,
                    s.keywords
                FROM document_sections ds
                JOIN document_versions dv ON dv.id = ds.document_version_id
                JOIN documents d ON d.id = dv.document_id
                LEFT JOIN document_section_summary s ON s.section_id = ds.id
                WHERE dv.s3_bucket = :bucket
                  AND (:tenant_id IS NULL OR d.tenant_id::text = :tenant_id)
                  {project_filter}
                  {section_filter}
                ORDER BY dv.s3_key, ds.section_number
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _gap_fetch_study_rows(db: Session, *, project_id: str) -> List[Dict[str, Any]]:
    """Gap fetch study rows."""
    rows = (
        db.execute(
            sqltext(
                """
                SELECT id, sponsor_study_id, study_type, glp_status, species, route,
                       duration_days, module4_section, extra_attributes, created_at
                FROM ncd_study
                WHERE project_id = :project_id
                ORDER BY created_at DESC NULLS LAST
                """
            ),
            {"project_id": project_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _gap_fetch_source_document_rows(
    db: Session, *, project_id: str
) -> List[Dict[str, Any]]:
    """Gap fetch source document rows."""
    rows = (
        db.execute(
            sqltext(
                """
                SELECT id, file_name, module, ctd_section, uploaded_at
                FROM ncd_source_document
                WHERE project_id = :project_id
                ORDER BY uploaded_at DESC NULLS LAST
                """
            ),
            {"project_id": project_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _gap_infer_tenant_id(
    db: Session, *, project_id: str, explicit_tenant_id: Optional[str]
) -> Optional[str]:
    """Gap infer tenant id."""
    if explicit_tenant_id:
        return explicit_tenant_id
    try:
        tenant = db.execute(
            sqltext("SELECT tenant_id::text FROM projects WHERE id = :project_id LIMIT 1"),
            {"project_id": project_id},
        ).scalar()
    except Exception:
        return None
    return str(tenant) if tenant else None


def _gap_check_project_prefix(
    *,
    bucket: str,
    project_name: Optional[str],
    project_prefix: Optional[str],
    aws_region: Optional[str],
) -> Dict[str, Any]:
    """Gap check project prefix."""
    requested_prefix = _gap_normalize_project_prefix(project_prefix)
    if requested_prefix:
        candidates = [requested_prefix]
    elif project_name:
        candidates = [
            _gap_normalize_project_prefix(project_name),
            _gap_normalize_project_prefix(f"filynai.com/{project_name}"),
        ]
    else:
        candidates = []

    if not candidates:
        return {
            "checked": False,
            "reason": "project_prefix_not_provided",
            "prefix": None,
            "object_count": None,
        }

    try:
        s3_client = _boto3_client("s3", region_name=aws_region)
    except Exception:
        logger.exception("S3 client unavailable during gap analysis prefix check")
        return {
            "checked": False,
            "reason": "s3_client_unavailable",
            "prefix": candidates[0],
            "object_count": None,
        }

    for candidate in candidates:
        try:
            result = s3_client.list_objects_v2(
                Bucket=bucket,
                Prefix=candidate,
                MaxKeys=50,
            )
        except Exception:
            logger.exception(
                "S3 list_objects_v2 failed during gap analysis prefix check for prefix=%s",
                candidate,
            )
            return {
                "checked": False,
                "reason": "s3_list_failed",
                "prefix": candidate,
                "object_count": None,
            }
        count = len(result.get("Contents") or [])
        if count:
            return {
                "checked": True,
                "reason": None,
                "prefix": candidate,
                "object_count": count,
            }

    return {
        "checked": True,
        "reason": None,
        "prefix": candidates[0],
        "object_count": 0,
    }


def _build_gap_analysis_report(
    db: Session,
    *,
    project_id: str,
    bucket: str,
    tenant_id: Optional[str],
    project_prefix: Optional[str],
    include_optional_p1: bool,
    aws_region: Optional[str],
) -> Dict[str, Any]:
    """Build gap analysis report."""
    project_name = fetch_project_name(db, project_id)
    effective_tenant = _gap_infer_tenant_id(
        db, project_id=project_id, explicit_tenant_id=tenant_id
    )

    required_sections = _gap_collect_required_sections(include_optional_p1)
    required_module4_sections = [
        str(item["module4_section"]) for item in required_sections
    ]

    diagnostics: List[str] = []
    try:
        document_rows = _gap_fetch_project_document_rows(
            db,
            bucket=bucket,
            tenant_id=effective_tenant,
            project_name=project_name,
            project_prefix=project_prefix,
        )
    except Exception:
        logger.exception("document_versions query failed during gap analysis")
        try:
            db.rollback()
        except Exception:
            logger.exception("Rollback failed after document_versions query failure")
        diagnostics.append("document_versions query failed")
        document_rows = []

    try:
        section_rows = _gap_fetch_section_rows(
            db,
            bucket=bucket,
            tenant_id=effective_tenant,
            project_name=project_name,
            project_prefix=project_prefix,
            module4_sections=required_module4_sections,
        )
    except Exception:
        logger.exception("document_sections query failed during gap analysis")
        try:
            db.rollback()
        except Exception:
            logger.exception("Rollback failed after document_sections query failure")
        diagnostics.append("document_sections query failed")
        section_rows = []

    try:
        study_rows = _gap_fetch_study_rows(db, project_id=project_id)
    except Exception:
        logger.exception("ncd_study query failed during gap analysis")
        try:
            db.rollback()
        except Exception:
            logger.exception("Rollback failed after ncd_study query failure")
        diagnostics.append("ncd_study query failed")
        study_rows = []

    try:
        source_document_rows = _gap_fetch_source_document_rows(db, project_id=project_id)
    except Exception:
        logger.exception("ncd_source_document query failed during gap analysis")
        try:
            db.rollback()
        except Exception:
            logger.exception("Rollback failed after ncd_source_document query failure")
        diagnostics.append("ncd_source_document query failed")
        source_document_rows = []

    keys = [str(row.get("s3_key") or "") for row in document_rows if row.get("s3_key")]
    prefix_check = _gap_check_project_prefix(
        bucket=bucket,
        project_name=project_name,
        project_prefix=project_prefix,
        aws_region=aws_region,
    )

    missing_files: List[Dict[str, Any]] = []
    missing_data_fields: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    section_reports: List[Dict[str, Any]] = []
    observed_sections: Set[str] = set()
    module_file_counts: Dict[str, int] = {module: 0 for module in _GAP_MODULES}
    module_extracted_counts: Dict[str, int] = {module: 0 for module in _GAP_MODULES}
    module_sample_keys: Dict[str, List[str]] = {module: [] for module in _GAP_MODULES}

    for row in document_rows:
        key = str(row.get("s3_key") or "")
        module = _gap_parse_module_number(key)
        if not module:
            continue
        module_file_counts[module] += 1
        if key and len(module_sample_keys[module]) < 5:
            module_sample_keys[module].append(key)

    for row in source_document_rows:
        module = _gap_parse_module_number(str(row.get("module") or ""))
        if not module:
            module = _gap_parse_module_number(str(row.get("file_name") or ""))
        if not module:
            continue
        module_file_counts[module] += 1
        file_name = str(row.get("file_name") or "")
        if file_name and len(module_sample_keys[module]) < 5:
            module_sample_keys[module].append(file_name)

    for row in section_rows:
        module = _gap_parse_module_number(str(row.get("section_number") or ""))
        if not module:
            continue
        module_extracted_counts[module] += 1

    for row in study_rows:
        module = _gap_parse_module_number(str(row.get("module4_section") or ""))
        if not module:
            continue
        module_extracted_counts[module] += 1

    for required in required_sections:
        module4_section = str(required["module4_section"])
        priority = str(required["priority"])
        required_fields = list(required.get("required_fields") or [])

        matching_key_rows = [
            row
            for row in document_rows
            if _gap_key_mentions_module4_section(str(row.get("s3_key") or ""), module4_section)
        ]
        matching_sources = [
            row
            for row in section_rows
            if _gap_section_matches(str(row.get("section_number") or ""), module4_section)
        ]
        matching_studies = [
            row
            for row in study_rows
            if _gap_section_matches(str(row.get("module4_section") or ""), module4_section)
        ]
        matching_source_docs = [
            row
            for row in source_document_rows
            if _gap_key_mentions_module4_section(str(row.get("file_name") or ""), module4_section)
            or _gap_section_matches(str(row.get("ctd_section") or ""), module4_section)
        ]

        has_files = bool(matching_key_rows or matching_source_docs)
        has_extracted = bool(matching_sources or matching_studies)
        if has_files or has_extracted:
            observed_sections.add(module4_section)

        section_issue_refs: List[str] = []
        if not has_files and not has_extracted:
            issue = {
                "type": "missing_module4_files",
                "severity": "critical" if priority == "P0" else "warning",
                "module4_section": module4_section,
                "priority": priority,
                "message": (
                    f"No files or extracted records found for required Module 4 "
                    f"section {module4_section}."
                ),
            }
            issues.append(issue)
            section_issue_refs.append(issue["type"])
            missing_file_payload = {
                "scope": "module4_section",
                "module4_section": module4_section,
                "priority": priority,
                "category": required.get("category"),
                "reason": "No matching files or extracted records",
            }
            missing_files.append(missing_file_payload)
        elif has_files and not has_extracted:
            issue = {
                "type": "uploaded_not_extracted",
                "severity": "warning",
                "module4_section": module4_section,
                "priority": priority,
                "message": (
                    f"Files detected for {module4_section}, but extracted section/study "
                    "records are missing."
                ),
            }
            issues.append(issue)
            section_issue_refs.append(issue["type"])

        corpus_parts: List[str] = []
        for row in matching_key_rows:
            corpus_parts.append(str(row.get("s3_key") or ""))
        for row in matching_source_docs:
            corpus_parts.append(str(row.get("file_name") or ""))
            corpus_parts.append(str(row.get("ctd_section") or ""))
        for row in matching_sources:
            corpus_parts.append(str(row.get("section_number") or ""))
            corpus_parts.append(str(row.get("section_title") or ""))
            corpus_parts.append(_gap_stringify(row.get("summary_text")))
            corpus_parts.append(_gap_stringify(row.get("keywords")))
        for row in matching_studies:
            corpus_parts.append(str(row.get("sponsor_study_id") or ""))
            corpus_parts.append(str(row.get("study_type") or ""))
            corpus_parts.append(str(row.get("species") or ""))
            corpus_parts.append(str(row.get("route") or ""))
            corpus_parts.append(str(row.get("glp_status") or ""))
            corpus_parts.append(_gap_stringify(row.get("extra_attributes")))
        section_corpus = " ".join(part for part in corpus_parts if part).strip()[:120000]

        missing_fields_for_section: List[str] = []
        if has_files or has_extracted:
            for field_name in required_fields:
                if not _gap_field_is_present(field_name, section_corpus):
                    missing_fields_for_section.append(field_name)

        if missing_fields_for_section:
            issue = {
                "type": "missing_required_data_fields",
                "severity": "critical" if priority == "P0" else "warning",
                "module4_section": module4_section,
                "priority": priority,
                "missing_count": len(missing_fields_for_section),
                "message": (
                    f"Missing {len(missing_fields_for_section)} required field(s) in "
                    f"content linked to {module4_section}."
                ),
                "fields": missing_fields_for_section,
            }
            issues.append(issue)
            section_issue_refs.append(issue["type"])
            missing_data_fields.append(
                {
                    "module4_section": module4_section,
                    "priority": priority,
                    "fields": missing_fields_for_section,
                }
            )

        section_reports.append(
            {
                "module4_section": module4_section,
                "priority": priority,
                "category": required.get("category"),
                "required_fields_count": len(required_fields),
                "missing_fields_count": len(missing_fields_for_section),
                "missing_fields": missing_fields_for_section,
                "file_evidence": {
                    "document_keys": [row.get("s3_key") for row in matching_key_rows],
                    "source_documents": [
                        row.get("file_name") for row in matching_source_docs
                    ],
                    "section_sources": len(matching_sources),
                    "studies": len(matching_studies),
                },
                "status": (
                    "missing_files"
                    if not has_files and not has_extracted
                    else "gaps_found"
                    if missing_fields_for_section
                    else "ok"
                ),
                "issue_types": section_issue_refs,
            }
        )

    module_coverage: List[Dict[str, Any]] = []
    missing_modules: List[str] = []
    for module in _GAP_MODULES:
        file_count = module_file_counts[module]
        extracted_count = module_extracted_counts[module]
        has_files = file_count > 0
        has_extracted = extracted_count > 0
        status = (
            "missing_files"
            if not has_files
            else "uploaded_not_extracted"
            if has_files and not has_extracted
            else "ok"
        )
        module_coverage.append(
            {
                "module": module,
                "has_files": has_files,
                "file_count": file_count,
                "has_extracted_data": has_extracted,
                "extracted_record_count": extracted_count,
                "sample_keys": module_sample_keys[module],
                "status": status,
            }
        )
        if not has_files:
            missing_modules.append(module)
            issues.append(
                {
                    "type": "missing_module_files",
                    "severity": "critical",
                    "module": module,
                    "module4_section": None,
                    "priority": "P0",
                    "message": f"No files detected for Module {module}.",
                }
            )
            missing_files.append(
                {
                    "scope": "module",
                    "module": module,
                    "priority": "P0",
                    "reason": f"Module {module} has no files in project scope",
                }
            )

    if (
        prefix_check.get("checked")
        and prefix_check.get("object_count") == 0
        and not keys
        and not source_document_rows
    ):
        issues.append(
            {
                "type": "project_folder_empty",
                "severity": "critical",
                "module4_section": None,
                "priority": "P0",
                "message": (
                    f"No files found under s3://{bucket}/{prefix_check.get('prefix')}"
                ),
            }
        )

    if not prefix_check.get("checked") and prefix_check.get("reason"):
        diagnostics.append(str(prefix_check.get("reason")))

    critical_count = sum(1 for issue in issues if issue.get("severity") == "critical")
    warning_count = sum(1 for issue in issues if issue.get("severity") == "warning")
    missing_module4_sections_count = sum(
        1 for row in missing_files if row.get("scope") == "module4_section"
    )
    return {
        "project_id": project_id,
        "bucket": bucket,
        "tenant_id": effective_tenant,
        "project_name": project_name,
        "project_prefix": _gap_normalize_project_prefix(project_prefix)
        or prefix_check.get("prefix"),
        "prefix_check": prefix_check,
        "required_module4_sections": required_module4_sections,
        "observed_module4_sections": sorted(observed_sections),
        "module_coverage": module_coverage,
        "missing_modules": missing_modules,
        "missing_files": missing_files,
        "missing_data_fields": missing_data_fields,
        "section_reports": section_reports,
        "issues": issues,
        "summary": {
            "required_modules": len(_GAP_MODULES),
            "missing_modules": len(missing_modules),
            "required_sections": len(required_sections),
            "observed_sections": len(observed_sections),
            "missing_files_total": len(missing_files),
            "missing_file_sections": missing_module4_sections_count,
            "sections_with_missing_fields": len(missing_data_fields),
            "issue_count": len(issues),
            "critical_issues": critical_count,
            "warning_issues": warning_count,
        },
        "diagnostics": diagnostics,
    }
