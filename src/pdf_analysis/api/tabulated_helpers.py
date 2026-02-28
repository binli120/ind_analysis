"""Tabulated/repair helpers and CTD context builders."""

from __future__ import annotations

import ast
import html
import json
import logging
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from fastapi import HTTPException
from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from ncd.database.db import SessionLocal
from ncd.types.ctd_elements import build_ctd_element_reference
from ncd.types.ctd_materials import (
    fetch_assets_for_sections,
    fetch_document_keys_for_sections,
    fetch_key_sections_for_sections,
    fetch_ncd_payload,
    fetch_project_document_keys,
    fetch_project_name,
    fetch_section_sources,
    fetch_study_ids_for_sections,
    module4_sections_for_ctd_targets,
    section_number_matches,
)
from pdf_analysis.api.constants import (
    SECTION_PROMPT_MAX_CHARS,
    STUDY_ID_EXT_RE,
    STUDY_ID_PREFIX_RE,
    STUDY_ID_RE,
    STUDY_ID_SKIP_RE,
    STUDY_ID_TRAILERS,
)
from pdf_analysis.api.gap_analysis import _gap_key_mentions_module4_section
from pdf_analysis.api.request_utils import _normalize_user_prompt
from pdf_analysis.api.s3_utils import (
    _boto3_client,
    _normalize_extra_attributes,
    _read_s3_text,
)
from pdf_analysis.api.table_utils import _parse_columns_header, _truncate_preview_value
from pdf_analysis.api.section_summary_helpers import (
    _element_entries_for_section,
    _filter_element_numbers_for_targets,
    _filter_mapping_entries_by_module4_sections,
    _filter_section_entries_for_targets,
    _format_embedding_for_prompt,
    _load_ind_template_entries,
    _mapped_target_sections_for_request,
    _normalize_summary,
    _sections_with_material_data,
    _slim_key_sections,
    _slim_sources,
    _summary_to_text,
    _template_entries_for_section,
    _trim_section_summary_context,
    _truncate_text,
)

logger = logging.getLogger(__name__)

__all__ = [
    "_build_section_summary_context",
    "_build_section_summary_prompt",
    "_trim_tabulated_context",
    "_extract_study_ids",
    "_canonical_study_number",
    "_build_study_id_candidates",
    "_select_study_id_from_text",
    "_normalize_tabulated_study_ids",
    "_normalize_header_token",
    "_tokenize_text",
    "_infer_project_prefix",
    "_list_module4_study_keys",
    "_pick_first_value",
    "_study_sort_key",
    "_extract_ncd_study_records",
    "_render_dose_group_summary",
    "_format_metric_value",
    "_summarize_exposure_metrics",
    "_summarize_findings",
    "_select_primary_safety_summary",
    "_build_document_detail_profiles",
    "_merge_text_segments",
    "_study_numbers_overlap",
    "_normalize_glp_value",
    "_infer_glp_compliance",
    "_extract_location_title",
    "_asset_single_study_id",
    "_infer_primary_pd_endpoints",
    "_infer_primary_pd_findings",
    "_looks_like_safety_pharmacology_text",
    "_infer_safety_organ_systems",
    "_asset_matches_safety_pharmacology",
    "_asset_matches_primary_pharmacology",
    "_extract_safety_sentences",
    "_synthesize_safety_findings",
    "_collect_safety_context_fragments",
    "_format_study_number_display",
    "_merge_safety_candidate_records",
    "_rows_from_token_candidates",
    "_extract_primary_pd_candidates",
    "_repair_primary_pharmacodynamics_table",
    "_extract_safety_pharmacology_candidates",
    "_repair_safety_pharmacology_table",
    "_first_non_empty",
    "_display_title_from_path",
    "_extract_overview_candidates_from_ncd",
    "_extract_overview_candidates",
    "_repair_overview_table",
    "_align_pharmacology_tabulated_specs",
    "_tabulated_template_entries_for_section",
    "_read_table_json_preview",
    "_build_tabulated_context",
    "_merge_tabulated_tables",
    "_normalize_tabulated_columns",
    "_columns_from_rows",
    "_realign_tabulated_tables_by_columns",
    "_build_tabulated_prompt",
    "_build_topic_title",
    "_attach_assets_to_topic",
    "_render_table_html",
]

_PHARMACOLOGY_263_COLUMN_OVERRIDES: Dict[str, List[str]] = {
    "2.6.3.1": [
        "Type of Study",
        "Test System",
        "Method of Administration",
        "Testing Facility",
        "Study Number",
    ],
    "2.6.3.2": [
        "Type of Study",
        "Species/Strain",
        "Method of Admin.",
        "Doses (mg/kg)",
        "Gender and No. per Group",
        "Noteworthy Findings",
        "Study Number",
    ],
    "2.6.3.3": ["Statement"],
    "2.6.3.4": [
        "Organ Systems Evaluated",
        "Species/Strain",
        "Method of Admin.",
        "Doses (mg/kg)",
        "Gender and No. per Group",
        "Noteworthy Findings",
        "GLP Compliance",
        "Study Number",
    ],
    "2.6.3.5": ["Statement"],
}

_PHARMACOLOGY_263_STATEMENTS: Dict[str, str] = {
    "2.6.3.3": "No secondary pharmacodynamics studies were conducted.",
    "2.6.3.5": "No pharmacodynamic drug interaction studies were conducted.",
}


def _align_pharmacology_tabulated_specs(
    section: str,
    table_specs: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Align 2.6.3 table specs to the legacy pharmacology tabulated layout."""
    if not section.startswith("2.6.3"):
        return [dict(spec) for spec in table_specs]

    aligned: List[Dict[str, Any]] = []
    for spec in table_specs:
        updated = dict(spec)
        subsection = str(updated.get("subsection") or "").strip()
        override_columns = _PHARMACOLOGY_263_COLUMN_OVERRIDES.get(subsection)
        if override_columns:
            updated["columns"] = list(override_columns)
            updated["columns_header"] = " | ".join(override_columns)
        aligned.append(updated)
    return aligned


def _populate_pharmacology_statement_tables(
    tables: Sequence[Dict[str, Any]],
) -> None:
    """Populate fixed statement rows for 2.6.3 subsections represented as narrative."""
    for table in tables:
        if not isinstance(table, dict):
            continue
        subsection = str(table.get("subsection") or "").strip()
        statement = _PHARMACOLOGY_263_STATEMENTS.get(subsection)
        if not statement:
            continue

        columns = [str(col) for col in (table.get("columns") or []) if str(col).strip()]
        if not columns:
            columns = ["Statement"]
            table["columns"] = columns
        statement_col = next(
            (
                col
                for col in columns
                if _normalize_header_token(col) == "statement"
            ),
            columns[0],
        )

        rows = table.get("rows")
        if not isinstance(rows, list):
            rows = []
            table["rows"] = rows
        if any(
            isinstance(row, dict) and any(str(value).strip() for value in row.values())
            for row in rows
        ):
            continue

        row = {col: "" for col in columns}
        row[statement_col] = statement
        table["rows"] = [row]
        if not str(table.get("notes") or "").strip():
            table["notes"] = statement


def _build_section_summary_context(
    db: Session,
    *,
    section: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
) -> tuple[Dict[str, Any], List[str]]:
    """Build section summary context."""
    project_name = fetch_project_name(db, project_id)
    project_like = f"%/{project_name}/%" if project_name else "%"

    module4_sections, mapping_entries, targets = module4_sections_for_ctd_targets(
        section
    )
    if section.startswith("2.6."):
        exact_sections = set()
        filtered_mappings: List[Dict[str, Any]] = []
        for entry in mapping_entries:
            matched_targets = [
                target_entry
                for target_entry in entry.get("targets", [])
                if target_entry.get("section") == section
            ]
            if matched_targets:
                filtered = dict(entry)
                filtered["matched_targets"] = matched_targets
                filtered_mappings.append(filtered)
                exact_sections.add(entry.get("module4_section"))
        if filtered_mappings:
            module4_sections = sorted(s for s in exact_sections if s)
            mapping_entries = filtered_mappings

    sources = fetch_section_sources(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
        module4_sections=module4_sections,
    )
    project_document_keys = fetch_project_document_keys(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
    )
    document_keys = fetch_document_keys_for_sections(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
        module4_sections=module4_sections,
    )
    key_sections = fetch_key_sections_for_sections(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
        module4_sections=module4_sections,
        section_type=None,
    )
    ncd_payload: Dict[str, Any] = {}
    try:
        ncd_payload = fetch_ncd_payload(
            db,
            project_id=project_id,
            module4_sections=module4_sections,
        )
    except Exception as exc:
        logger.warning(
            "section summary ctx: failed to fetch ncd payload for section=%s project=%s: %s",
            section,
            project_id,
            exc,
        )
        ncd_payload = {}

    available_sections = _sections_with_material_data(
        module4_sections,
        section_sources=sources,
        document_keys=document_keys,
        ncd_payload=ncd_payload,
    )
    if set(available_sections) != set(module4_sections):
        module4_sections = available_sections
        mapping_entries = _filter_mapping_entries_by_module4_sections(
            mapping_entries,
            module4_sections,
        )
        sources = fetch_section_sources(
            db,
            tenant_id=tenant_id,
            bucket=bucket,
            project_like=project_like,
            module4_sections=module4_sections,
        )
        document_keys = fetch_document_keys_for_sections(
            db,
            tenant_id=tenant_id,
            bucket=bucket,
            project_like=project_like,
            module4_sections=module4_sections,
        )
        key_sections = fetch_key_sections_for_sections(
            db,
            tenant_id=tenant_id,
            bucket=bucket,
            project_like=project_like,
            module4_sections=module4_sections,
            section_type=None,
        )
        try:
            ncd_payload = fetch_ncd_payload(
                db,
                project_id=project_id,
                module4_sections=module4_sections,
            )
        except Exception as exc:
            logger.warning(
                "section summary ctx: failed to refetch ncd payload for section=%s project=%s: %s",
                section,
                project_id,
                exc,
            )
            ncd_payload = {}

    mapped_target_sections = _mapped_target_sections_for_request(
        section=section,
        mapping_entries=mapping_entries,
    )
    template_entries = _template_entries_for_section(section)
    element_numbers = _element_entries_for_section(section)
    if section.startswith("2.6."):
        template_entries = _filter_section_entries_for_targets(
            template_entries,
            target_sections=mapped_target_sections,
        )
        element_numbers = _filter_element_numbers_for_targets(
            element_numbers,
            target_sections=mapped_target_sections,
        )

    section_is_pk = section.startswith("2.6.4") or section.startswith("2.6.5")
    section_is_tox = (
        section.startswith("2.6.6")
        or section.startswith("2.6.7")
        or section.startswith("2.4.4")
    )
    table_specs: List[Dict[str, Any]] = []
    table_assets: List[Dict[str, Any]] = []
    if section.startswith("2.4") or section.startswith("2.6"):
        max_table_rows = 4
        max_tables = 8
        if section_is_pk:
            max_table_rows = 8
            max_tables = 16
        elif section_is_tox:
            max_table_rows = 6
            max_tables = 12
        try:
            tabulated_context, _ = _build_tabulated_context(
                db,
                section=section,
                tenant_id=tenant_id,
                project_id=project_id,
                bucket=bucket,
                max_table_rows=max_table_rows,
                max_tables=max_tables,
            )
            table_specs = tabulated_context.get("table_specs") or []
            table_assets = tabulated_context.get("table_assets") or []
        except Exception as exc:
            logger.warning(
                "section summary ctx: failed to load tabulated context for section=%s project=%s: %s",
                section,
                project_id,
                exc,
            )

    element_payloads: List[Dict[str, Any]] = []
    used_elements: List[str] = []
    for element in element_numbers:
        try:
            payload = build_ctd_element_reference(
                db,
                tenant_id=tenant_id,
                project_id=project_id,
                bucket=bucket,
                element_number=element,
                include_assets=False,
                include_key_sections=True,
            )
        except ValueError:
            continue
        element_payloads.append(
            {
                "element_number": payload.get("element_number"),
                "section_number": payload.get("section_number"),
                "module4_sections": payload.get("module4_sections") or [],
                "template": payload.get("template") or {},
                "sources": _slim_sources(payload.get("sources") or []),
                "key_sections": _slim_key_sections(payload.get("key_sections") or []),
            }
        )
        if payload.get("element_number"):
            used_elements.append(str(payload.get("element_number")))

    context = {
        "section": section,
        "ctd_targets": targets,
        "module4_sections": module4_sections,
        "mapping": mapping_entries,
        "template_entries": template_entries,
        "section_sources": _slim_sources([dict(row) for row in sources]),
        "section_key_sections": _slim_key_sections([dict(row) for row in key_sections]),
        "table_specs": table_specs,
        "table_assets": table_assets,
        "document_keys": document_keys,
        "project_document_keys": project_document_keys,
        "ncd_payload": ncd_payload,
        "elements": element_payloads,
    }
    context["document_detail_profiles"] = _build_document_detail_profiles(context)
    return context, used_elements


def _build_section_summary_prompt(
    *,
    section: str,
    context: Dict[str, Any],
    user_prompt: Optional[str],
    user_comment: Optional[str],
    previous_summary: Optional[str],
    previous_embedding: Any,
) -> tuple[str, str]:
    """Build section summary prompt."""
    user_prompt = _normalize_user_prompt(user_prompt)
    section_is_pk = section.startswith("2.6.4") or section.startswith("2.6.5")
    system_prompt = (
        "You are an expert nonclinical regulatory writer. "
        "Generate CTD Module 2.4/2.6 section summaries using only the provided data. "
        "Do not invent data. If key data is missing, state what is missing. "
        "If numeric outcomes are present (dose levels, fold changes, % inhibition, p-values, Cmax/AUC), include them. "
        "For PK sections (2.6.4/2.6.5), report available numeric PK values by study when present "
        "(for example half-life, Cmax, AUC, CL, Vss, bioavailability, urinary/fecal recovery). "
        "Do not claim that data is missing when it appears in the provided context. "
        "Only state that a parameter is unavailable when no corresponding value appears anywhere in the provided context. "
        "Preserve mixed or negative findings and explicitly label non-significant outcomes when reported. "
        "Use exact numbers from the source text only; do not round, average, or merge non-equivalent values. "
        "If studies report different values, present them separately by study and note the discrepancy. "
        "When section_sources include numeric_evidence, prioritize those numbers over paraphrased narrative text. "
        "When table_numeric_evidence is present, treat it as source-of-truth numeric evidence and use it directly with units. "
        "When document_detail_profiles are present, use them as the primary study-level source for dose groups, "
        "PK/TK parameters, findings, NOAEL/LOAEL, and GLP context. "
        "If a profile marks missing_detail_fields, report that as a gap rather than inferring missing values. "
        "Do not state a PK parameter is unavailable when a value for that parameter appears in table_numeric_evidence. "
        "Write in concise CTD dossier prose (paragraphs with clear study-result statements), avoiding unnecessary outline labels."
    )
    if section_is_pk:
        system_prompt += (
            " For PK sections (2.6.4/2.6.5), preserve subsection structure and present study-by-study numeric findings "
            "for dose, route, half-life, Cmax, AUC, CL, Vss, and bioavailability whenever present."
        )
    instructions = (
        user_prompt
        if user_prompt
        else (
            "Use the template guidance and data to produce a concise section summary."
            if not section_is_pk
            else (
                "Use the template guidance and data to produce a subsection-aligned PK summary that stays close "
                "to the source table wording and numeric values."
            )
        )
    )
    parts = [
        f"CTD Section: {section}",
        f"Instructions: {instructions}",
    ]
    if user_comment:
        if previous_summary:
            parts.append(f"Previous summary:\n{previous_summary}")
        embedding_text = _format_embedding_for_prompt(previous_embedding)
        if embedding_text:
            parts.append(f"Previous summary embedding: {embedding_text}")
        parts.append(f"User comment:\n{user_comment}")
        parts.append("Revise the summary to address the comment.")

    context_json = json.dumps(
        _trim_section_summary_context(context), ensure_ascii=True, indent=2, default=str
    )
    parts.append(f"Context data (JSON):\n{context_json}")
    return system_prompt, "\n\n".join(parts)


def _trim_tabulated_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """Trim tabulated context."""
    section = str(context.get("section") or "")
    if section.startswith("2.6.3"):
        max_assets = 24
        max_preview_rows = 8
    else:
        max_assets = 8
        max_preview_rows = 6
    max_columns = 12
    trimmed_assets: List[Dict[str, Any]] = []
    for asset in (context.get("table_assets") or [])[:max_assets]:
        if not isinstance(asset, dict):
            continue
        preview_rows = []
        for row in (asset.get("preview_rows") or [])[:max_preview_rows]:
            if not isinstance(row, dict):
                continue
            preview_rows.append({k: _truncate_preview_value(v) for k, v in row.items()})
        trimmed_assets.append(
            {
                "id": asset.get("id"),
                "s3_key": asset.get("s3_key"),
                "json_key": asset.get("json_key"),
                "caption": asset.get("caption"),
                "description": asset.get("description"),
                "page_number": asset.get("page_number"),
                "index_on_page": asset.get("index_on_page"),
                "columns": (asset.get("columns") or [])[:max_columns],
                "row_count": asset.get("row_count"),
                "preview_rows": preview_rows,
            }
        )
    trimmed_sources: List[Dict[str, Any]] = []
    for source in (context.get("section_sources") or [])[:30]:
        if not isinstance(source, dict):
            continue
        trimmed_sources.append(
            {
                "section_number": source.get("section_number"),
                "section_title": source.get("section_title"),
                "summary_text": _truncate_text(
                    str(source.get("summary_text") or ""), max_chars=1200
                ),
                "keywords": (source.get("keywords") or [])[:15],
            }
        )
    payload = context.get("ncd_payload") or {}
    ncd_studies = []
    if isinstance(payload, dict):
        for study in (payload.get("studies") or [])[:120]:
            if not isinstance(study, dict):
                continue
            extra = _normalize_extra_attributes(study.get("extra_attributes"))
            ncd_studies.append(
                {
                    "sponsor_study_id": study.get("sponsor_study_id"),
                    "study_type": study.get("study_type"),
                    "glp_status": study.get("glp_status"),
                    "species": study.get("species"),
                    "strain": study.get("strain"),
                    "route": study.get("route"),
                    "module4_section": study.get("module4_section"),
                    "extra_attributes": {
                        "type_of_study": extra.get("type_of_study"),
                        "study_title": extra.get("study_title"),
                        "study_number_display": extra.get("study_number_display"),
                        "sponsor_study_number": extra.get("sponsor_study_number"),
                        "cro_study_number": extra.get("cro_study_number"),
                        "test_system": extra.get("test_system"),
                        "species_strain": extra.get("species_strain"),
                        "method_of_administration": extra.get(
                            "method_of_administration"
                        ),
                        "dose": extra.get("dose"),
                        "dose_level": extra.get("dose_level"),
                        "dose_levels": extra.get("dose_levels"),
                        "endpoints": extra.get("endpoints"),
                        "assays": extra.get("assays"),
                        "key_findings": extra.get("key_findings"),
                        "noteworthy_findings": extra.get("noteworthy_findings"),
                        "organ_systems": extra.get("organ_systems"),
                        "glp_compliance": extra.get("glp_compliance"),
                        "testing_facility": extra.get("testing_facility"),
                        "location_in_ctd": extra.get("location_in_ctd"),
                    },
                }
            )
    return {
        "section": context.get("section"),
        "ctd_targets": context.get("ctd_targets") or [],
        "module4_sections": (context.get("module4_sections") or [])[:20],
        "table_specs": context.get("table_specs") or [],
        "table_assets": trimmed_assets,
        "section_sources": trimmed_sources,
        "ncd_studies": ncd_studies,
    }


def _extract_study_ids(text: str) -> List[str]:
    """Extract study ids."""
    if not text:
        return []
    found: List[str] = []
    for match in STUDY_ID_RE.findall(text):
        if not match:
            continue
        cleaned = STUDY_ID_EXT_RE.sub("", match)
        cleaned = STUDY_ID_PREFIX_RE.sub("", cleaned)
        cleaned = cleaned.strip("._-")
        if not cleaned:
            continue
        while "." in cleaned:
            parts = cleaned.split(".")
            if parts[-1].lower() not in STUDY_ID_TRAILERS:
                break
            cleaned = ".".join(parts[:-1]).strip("._-")
            if not cleaned:
                break
        if not cleaned:
            continue
        if STUDY_ID_SKIP_RE.match(cleaned):
            continue
        if len(cleaned) < 6:
            continue
        if sum(1 for ch in cleaned if ch.isdigit()) < 2:
            continue
        if not STUDY_ID_RE.fullmatch(cleaned):
            continue
        if any(ch.isalpha() for ch in cleaned):
            cleaned = cleaned.upper()
        found.append(cleaned)
    seen = set()
    ordered: List[str] = []
    for value in found:
        key = value.upper()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(value)
    return ordered


def _canonical_study_number(value: str) -> str:
    """Canonical study number."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = _extract_study_ids(raw)
    if parsed:
        return parsed[0]
    if any(ch.isalpha() for ch in raw):
        raw = raw.upper()
    if STUDY_ID_SKIP_RE.match(raw):
        return ""
    # Keep numeric sponsor/report IDs (for example, "600210" or "1006-2525")
    # that are common in legacy safety pharmacology reports.
    if re.fullmatch(r"\d{5,}", raw):
        return raw
    if re.fullmatch(r"\d{2,}(?:[-./]\d{2,})+", raw):
        return raw
    # Keep sponsor IDs that don't match strict regex (e.g., "WKP00013 Page 2"),
    # but avoid generic labels and weak fragments.
    digit_count = sum(1 for ch in raw if ch.isdigit())
    alpha_count = sum(1 for ch in raw if ch.isalpha())
    if digit_count >= 2 and alpha_count >= 2 and len(raw) >= 6:
        return raw
    return ""


_INTERNAL_DOCUMENT_STUDY_RE = re.compile(r"^DOC-\d+-[A-F0-9]{6,}$", re.IGNORECASE)
_EXTRACTION_PATH_RE = re.compile(
    r"\b(?:https?://|s3://|filynai\.com/)\S+|\b\S+\.pdf(?:\.(?:tables|images))?(?:/\S+)?",
    re.IGNORECASE,
)
_EXTRACTION_TRUNCATED_RE = re.compile(r"\.\.\.\[truncated\]", re.IGNORECASE)
_PLACEHOLDER_VALUE_RE = re.compile(
    r"^(?:n/?a|none|not reported|not available|not communicated|unknown|nil)$",
    re.IGNORECASE,
)


def _extract_summary_from_structured_text(text: str) -> str:
    """Extract nested summary text from JSON/Python-literal payload strings."""
    if not text:
        return ""
    candidate = text.strip()
    if "summary" not in candidate.lower():
        return ""

    def _extract_summary(obj: Any) -> str:
        if isinstance(obj, dict):
            summary = obj.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
            if isinstance(summary, dict):
                nested = _extract_summary(summary)
                if nested:
                    return nested
        return ""

    original = candidate
    for _ in range(2):
        parsed_summary = ""
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(candidate)
            except Exception:
                continue
            parsed_summary = _extract_summary(parsed)
            if parsed_summary:
                candidate = parsed_summary.strip()
                break
        if not parsed_summary:
            break

    if candidate and candidate != original:
        return candidate

    summary_match = re.search(
        r"""['"]summary['"]\s*:\s*['"](.+?)['"]\s*(?:,|})""",
        original,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if summary_match:
        return summary_match.group(1).strip()
    return ""


def _clean_extracted_text(value: Any) -> str:
    """Clean OCR/asset extraction artifacts from free text fields."""
    text = html.unescape(str(value or ""))
    if not text:
        return ""
    structured_summary = _extract_summary_from_structured_text(text)
    if structured_summary:
        text = structured_summary
    text = _EXTRACTION_PATH_RE.sub(" ", text)
    text = _EXTRACTION_TRUNCATED_RE.sub(" ", text)
    text = re.sub(r"(?:(?<=^)|(?<=[;,\s]))\d{1,2}\s*:\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ;,")
    return text


def _is_noisy_extraction_fragment(value: Any) -> bool:
    """Detect obvious extraction noise snippets that should not feed findings."""
    text = str(value or "").strip()
    if not text:
        return True
    lowered = text.lower()
    if "pdf.tables" in lowered or "longooc.com/" in lowered or "[truncated]" in lowered:
        return True
    if lowered.startswith("xml files/"):
        return True
    return False


def _is_placeholder_value(value: Any) -> bool:
    """Return True when value is a placeholder rather than meaningful content."""
    text = str(value or "").strip()
    if not text:
        return True
    normalized = re.sub(r"\s+", " ", text).strip()
    if _PLACEHOLDER_VALUE_RE.match(normalized):
        return True
    if re.fullmatch(r"study\s*#?:?\s*(?:n/?a|not reported)", normalized, flags=re.IGNORECASE):
        return True
    return False


def _select_preferred_study_number(*values: Any) -> str:
    """Select preferred study number, avoiding internal document IDs when possible."""
    ordered: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        canonical = _canonical_study_number(text)
        if canonical:
            ordered.append(canonical)
        ids = _extract_study_ids(text)
        if ids:
            ordered.extend(ids)
    if not ordered:
        return ""
    seen: Set[str] = set()
    deduped: List[str] = []
    for candidate in ordered:
        key = candidate.upper()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    for candidate in deduped:
        if _INTERNAL_DOCUMENT_STUDY_RE.match(candidate):
            continue
        return candidate
    return deduped[0]


def _build_study_id_candidates(context: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Build study id candidates."""
    candidates: Dict[str, Dict[str, Any]] = {}

    def add_candidate(study_id: str, label: str) -> None:
        """Add candidate."""
        key = study_id.upper()
        entry = candidates.setdefault(key, {"study_id": study_id, "labels": set()})
        if label:
            entry["labels"].add(label)

    for asset in context.get("table_assets", []) or []:
        if not isinstance(asset, dict):
            continue
        parts = [
            asset.get("s3_key"),
            asset.get("json_key"),
            asset.get("caption"),
            asset.get("description"),
        ]
        keywords = asset.get("keywords") or []
        if isinstance(keywords, list):
            parts.extend(str(item) for item in keywords if item)
        blob = " ".join(str(part) for part in parts if part)
        for study_id in _extract_study_ids(blob):
            add_candidate(study_id, blob)

    for source in context.get("section_sources", []) or []:
        if not isinstance(source, dict):
            continue
        blob = " ".join(
            str(part)
            for part in (
                source.get("section_title"),
                source.get("section_number"),
                source.get("s3_key"),
                source.get("summary_text"),
            )
            if part
        )
        for study_id in _extract_study_ids(blob):
            add_candidate(study_id, blob)

    for key in context.get("document_keys", []) or []:
        if not key:
            continue
        for study_id in _extract_study_ids(str(key)):
            add_candidate(study_id, key)

    for key in context.get("s3_listing_keys", []) or []:
        if not key:
            continue
        for study_id in _extract_study_ids(str(key)):
            add_candidate(study_id, key)

    for study_id in context.get("ncd_study_ids", []) or []:
        if not study_id:
            continue
        add_candidate(str(study_id), "ncd_study")

    return candidates


def _select_study_id_from_text(
    text: str, candidates: Dict[str, Dict[str, Any]]
) -> Optional[str]:
    """Select study id from text."""
    if not text:
        return None
    parsed_ids: List[str] = []
    for study_id in _extract_study_ids(text):
        parsed_ids.append(study_id)
        key = study_id.upper()
        if key in candidates:
            return candidates[key]["study_id"]

    best_id = None
    best_score = 0
    for entry in candidates.values():
        labels = entry.get("labels") or []
        for label in labels:
            score = fuzz.token_set_ratio(text, label)
            if score > best_score:
                best_score = score
                best_id = entry.get("study_id")
    if best_id and best_score >= 70:
        return best_id
    if parsed_ids:
        return parsed_ids[0]
    return None


def _normalize_tabulated_study_ids(
    tables: Sequence[Dict[str, Any]],
    context: Dict[str, Any],
) -> None:
    """Normalize tabulated study ids."""
    candidates = _build_study_id_candidates(context)
    if not candidates:
        return

    def pick_column(columns: Sequence[str], options: Sequence[str]) -> Optional[str]:
        """Pick column."""
        for column in columns:
            normalized = column.strip().lower()
            for option in options:
                if normalized == option:
                    return column
        return None

    for table in tables:
        if not isinstance(table, dict):
            continue
        columns = table.get("columns") or []
        if not isinstance(columns, list):
            continue
        study_col = pick_column(
            columns,
            (
                "study number",
                "study id",
                "study no.",
                "study no",
                "study #",
                "study number [-]",
                "study id [-]",
            ),
        )
        if not study_col:
            continue
        location_col = pick_column(
            columns,
            (
                "location in ctd",
                "location in ctd: vol. section",
                "location in ctd: vol. section [-]",
                "location in ctd [-]",
            ),
        )
        for row in table.get("rows") or []:
            if not isinstance(row, dict):
                continue
            current = str(row.get(study_col) or "").strip()
            if current and current.lower() in {
                "none",
                "none conducted",
                "not conducted",
            }:
                continue
            if current and _extract_study_ids(current):
                normalized = _select_study_id_from_text(current, candidates)
                if normalized:
                    row[study_col] = normalized
                continue
            location_value = (
                str(row.get(location_col) or "").strip() if location_col else ""
            )
            normalized = _select_study_id_from_text(location_value, candidates)
            if not normalized:
                row_text = " ".join(
                    str(row.get(col) or "") for col in columns if col != study_col
                ).strip()
                normalized = _select_study_id_from_text(row_text, candidates)
            if normalized:
                row[study_col] = normalized


def _normalize_header_token(value: str) -> str:
    """Normalize header token."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    alias_map = {
        "dose": "doses",
        "dose level": "doses",
        "dose levels": "doses",
        "dose mg kg": "doses",
        "dose mg/kg": "doses",
        "doses mg kg": "doses",
        "doses mg/kg": "doses",
        "dosage": "doses",
        "dose concentration": "dose concentration",
        "concentration": "dose concentration",
        "route of administration": "method of administration",
        "route": "method of administration",
        "administration route": "method of administration",
        "method of admin": "method of administration",
        "endpoint": "endpoints assays",
        "endpoints": "endpoints assays",
        "assay": "endpoints assays",
        "assays": "endpoints assays",
        "endpoint assay": "endpoints assays",
        "endpoint assays": "endpoints assays",
        "endpoints assay": "endpoints assays",
        "endpoints assays": "endpoints assays",
        "study id": "study number",
        "study no": "study number",
        "study no.": "study number",
        "study #": "study number",
        "study number no": "study number",
        "report study no": "study number",
        "report study number": "study number",
        "report number": "study number",
        "species strain": "species strain",
        "species strain or test system": "species strain or test system",
        "species strain or test system or species": "species strain or test system",
        "organ system": "organ systems evaluated",
        "organ systems": "organ systems evaluated",
        "systems evaluated": "organ systems evaluated",
        "system evaluated": "organ systems evaluated",
        "organ systems assessed": "organ systems evaluated",
        "target organ": "organ systems evaluated",
        "target organs": "organ systems evaluated",
        "safety pharmacology domain": "organ systems evaluated",
        "organ systems evaluated": "organ systems evaluated",
        "glp": "glp compliance",
        "glp status": "glp compliance",
        "glp statement": "glp compliance",
        "qa statement": "glp compliance",
        "compliance with glp": "glp compliance",
        "sex": "gender",
        "gender and no per group": "gender and no per group",
        "gender and no. per group": "gender and no per group",
        "sex and no per group": "gender and no per group",
        "sex and no. per group": "gender and no per group",
        "noteworthy findings": "noteworthy findings",
        "key findings": "noteworthy findings",
        "key results": "noteworthy findings",
        "findings": "noteworthy findings",
        "observations": "noteworthy findings",
        "conclusion": "noteworthy findings",
        "conclusions": "noteworthy findings",
        "result summary": "noteworthy findings",
    }
    if cleaned in alias_map:
        return alias_map[cleaned]
    if cleaned == "method of admin":
        return "method of administration"
    if cleaned == "method of administrationistration":
        return "method of administration"
    if cleaned == "method of admin istration":
        return "method of administration"
    if cleaned.startswith("dose "):
        return "doses"
    if cleaned.startswith("doses "):
        return "doses"
    if cleaned.startswith("organ system"):
        return "organ systems evaluated"
    if re.search(r"\borgan\b.*\bsystem", cleaned):
        return "organ systems evaluated"
    return cleaned


def _tokenize_text(value: str) -> set[str]:
    """Tokenize text."""
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _infer_project_prefix(
    project_name: Optional[str], seeds: Sequence[str]
) -> Optional[str]:
    """Infer project prefix."""
    if not project_name or not seeds:
        return None
    needle = f"/{project_name.lower()}/"
    for key in seeds:
        if not key:
            continue
        lower = str(key).lower()
        idx = lower.find(needle)
        if idx == -1:
            continue
        end = idx + len(needle)
        return str(key)[:end]
    return None


def _list_module4_study_keys(
    s3_client: Any,
    *,
    bucket: str,
    project_name: Optional[str],
    module4_sections: Sequence[str],
    seeds: Sequence[str],
    max_keys: int = 2000,
) -> List[str]:
    """List module4 study keys."""
    prefix = _infer_project_prefix(project_name, seeds)
    if not prefix or not module4_sections:
        return []
    search_prefix = f"{prefix}Module 4 Nonclinical Study Reports/"
    keys: List[str] = []
    key_set: set[str] = set()
    token: Optional[str] = None
    scanned = 0
    module_tokens = [str(section) for section in module4_sections if section]

    def add_key(key: str) -> None:
        """Add key."""
        if key in key_set:
            return
        key_set.add(key)
        keys.append(key)

    def list_prefix(prefix_value: str) -> None:
        """List prefix."""
        nonlocal scanned
        continuation: Optional[str] = None
        while True:
            params = {
                "Bucket": bucket,
                "Prefix": prefix_value,
                "MaxKeys": 1000,
            }
            if continuation:
                params["ContinuationToken"] = continuation
            try:
                response = s3_client.list_objects_v2(**params)
            except Exception:
                return
            contents = response.get("Contents") or []
            for item in contents:
                key = item.get("Key")
                if not key:
                    continue
                scanned += 1
                if scanned > max_keys:
                    return
                if module_tokens and not any(token in key for token in module_tokens):
                    continue
                if _extract_study_ids(key):
                    add_key(key)
            if not response.get("IsTruncated"):
                break
            continuation = response.get("NextContinuationToken")
            if not continuation:
                break

    seed_prefixes: set[str] = set()
    for seed in seeds:
        if not seed:
            continue
        parts = str(seed).split("/")
        for idx, segment in enumerate(parts):
            if not any(token in segment for token in module_tokens):
                continue
            candidate = "/".join(parts[: idx + 1]) + "/"
            if candidate.startswith(prefix):
                seed_prefixes.add(candidate)
    for seed_prefix in sorted(seed_prefixes):
        list_prefix(seed_prefix)
        if scanned > max_keys:
            return keys

    list_prefix(search_prefix)
    return keys


def _pick_first_value(
    row: Dict[str, Any], key_map: Dict[str, str], *candidates: str
) -> str:
    """Pick first value."""
    for candidate in candidates:
        key = key_map.get(candidate)
        if key:
            return str(row.get(key) or "").strip()
    return ""


def _study_sort_key(study_number: str) -> Tuple[Any, ...]:
    """Study sort key."""
    tokens = re.split(r"(\d+)", (study_number or "").upper())
    key: List[Tuple[int, Any]] = []
    for token in tokens:
        if not token:
            continue
        if token.isdigit():
            key.append((0, int(token)))
        else:
            key.append((1, token))
    return tuple(key)


def _extract_ncd_study_records(
    context: Dict[str, Any],
    *,
    module4_section: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Extract ncd study records."""
    payload = context.get("ncd_payload") or {}
    if not isinstance(payload, dict):
        return []

    studies = payload.get("studies") or []
    source_documents = payload.get("source_documents") or []
    if not isinstance(studies, list):
        studies = []
    if not isinstance(source_documents, list):
        source_documents = []

    category_by_module: Dict[str, str] = {}
    for entry in context.get("mapping") or []:
        if not isinstance(entry, dict):
            continue
        module4 = str(entry.get("module4_section") or "").strip()
        category = str(entry.get("category") or "").strip()
        if module4 and category:
            category_by_module[module4] = category

    source_by_id: Dict[str, Dict[str, Any]] = {}
    source_by_study: Dict[str, Dict[str, Any]] = {}
    for source in source_documents:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("id") or "").strip()
        if source_id:
            source_by_id[source_id] = source
        blob = " ".join(
            str(part)
            for part in (
                source.get("file_name"),
                source.get("ctd_section"),
                source.get("module"),
            )
            if part
        )
        for study_id in _extract_study_ids(blob):
            source_by_study.setdefault(study_id.upper(), source)

    document_keys_by_study: Dict[str, str] = {}
    for key in (context.get("document_keys") or []) + (
        context.get("project_document_keys") or []
    ):
        text = str(key or "").strip()
        if not text:
            continue
        for study_id in _extract_study_ids(text):
            document_keys_by_study.setdefault(study_id.upper(), text)

    records_by_study: Dict[str, Dict[str, Any]] = {}
    for study in studies:
        if not isinstance(study, dict):
            continue
        extra = _normalize_extra_attributes(study.get("extra_attributes"))
        study_number = _select_preferred_study_number(
            study.get("sponsor_study_id"),
            study.get("study_number"),
            extra.get("sponsor_study_number"),
            extra.get("study_number"),
            extra.get("study_number_display"),
            extra.get("cro_study_number"),
        )
        if not study_number:
            continue
        normalized_key = study_number.upper()

        source_doc_id = _first_non_empty(
            (
                study.get("main_source_document_id"),
                extra.get("legacy_document_id"),
            )
        )
        source_doc = source_by_id.get(source_doc_id)
        if source_doc is None:
            source_doc = source_by_study.get(normalized_key)

        module4 = _first_non_empty(
            (
                study.get("module4_section"),
                source_doc.get("ctd_section") if isinstance(source_doc, dict) else "",
            )
        )
        if module4_section and not section_number_matches(module4, module4_section):
            continue

        source_title = ""
        if isinstance(source_doc, dict):
            source_title = _display_title_from_path(str(source_doc.get("file_name") or ""))
        if not source_title:
            source_title = _display_title_from_path(
                document_keys_by_study.get(normalized_key, "")
            )
        location_in_ctd = _first_non_empty(
            (
                extra.get("location_in_ctd"),
                (
                    f'Module 4, Section {module4}: "{source_title}"'
                    if module4 and source_title
                    else ""
                ),
                f"Module 4, Section {module4}" if module4 else "",
            )
        )

        species = str(study.get("species") or "").strip()
        strain = str(study.get("strain") or "").strip()
        test_system = _first_non_empty(
            (
                extra.get("test_system"),
                extra.get("species_strain"),
                extra.get("species_or_system"),
                f"{species}; {strain}" if species and strain else "",
                species,
            )
        )

        record = {
            "study_id": str(study.get("id") or "").strip(),
            "study_number": study_number,
            "module4_section": module4,
            "type_of_study": _first_non_empty(
                (
                    extra.get("type_of_study"),
                    study.get("study_type"),
                    extra.get("study_title"),
                    extra.get("title"),
                    category_by_module.get(module4),
                )
            ),
            "species": species,
            "strain": strain,
            "test_system": test_system,
            "method_of_administration": _first_non_empty(
                (
                    extra.get("method_of_administration"),
                    extra.get("route_of_administration"),
                    extra.get("route"),
                    study.get("route"),
                )
            ),
            "testing_facility": _first_non_empty(
                (
                    extra.get("testing_facility"),
                    extra.get("test_facility"),
                    extra.get("facility"),
                    extra.get("laboratory"),
                )
            ),
            "glp_compliance": _first_non_empty(
                (
                    _normalize_glp_value(study.get("glp_status")),
                    _normalize_glp_value(extra.get("glp_compliance")),
                    _normalize_glp_value(extra.get("glp_status")),
                    _normalize_glp_value(extra.get("glp")),
                )
            ),
            "location_in_ctd": location_in_ctd,
            "extra": extra,
        }
        score = sum(
            1
            for field in (
                "type_of_study",
                "test_system",
                "method_of_administration",
                "testing_facility",
                "glp_compliance",
                "location_in_ctd",
            )
            if str(record.get(field) or "").strip()
        )
        existing = records_by_study.get(normalized_key)
        if existing is not None and int(existing.get("_score", 0)) >= score:
            continue
        record["_score"] = score
        records_by_study[normalized_key] = record

    records = [
        {k: v for k, v in record.items() if k != "_score"}
        for record in records_by_study.values()
    ]
    records.sort(key=lambda record: _study_sort_key(str(record.get("study_number") or "")))
    return records


def _render_dose_group_summary(groups: Sequence[Dict[str, Any]]) -> Tuple[str, str]:
    """Render dose group summary."""
    def _compact_number(value: Any) -> str:
        """Compact number."""
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            number = Decimal(text)
        except (InvalidOperation, ValueError):
            return text
        normalized = number.normalize()
        if normalized == normalized.to_integral():
            return str(normalized.quantize(Decimal("1")))
        formatted = format(normalized, "f").rstrip("0").rstrip(".")
        return formatted or "0"

    def _sex_label(value: str) -> str:
        """Sex label."""
        cleaned = str(value or "").strip()
        if not cleaned:
            return ""
        upper = cleaned.upper()
        if upper in {"M", "MALE"}:
            return "Male"
        if upper in {"F", "FEMALE"}:
            return "Female"
        if upper in {"M/F", "F/M", "MALE/FEMALE", "FEMALE/MALE"}:
            return "Male/Female"
        return cleaned

    doses: List[str] = []
    dose_seen: Set[str] = set()
    sex_profiles: List[Tuple[str, str]] = []
    sex_seen: Set[Tuple[str, str]] = set()
    control_present = False
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or "").strip().lower()
        if "vehicle" in group_name or "control" in group_name:
            control_present = True
        dose_value = group.get("dose_mg_per_kg")
        if dose_value is not None and str(dose_value).strip() != "":
            dose_text = _compact_number(dose_value)
            if dose_text in {"0", "0.0"}:
                control_present = True
            elif dose_text and dose_text not in dose_seen:
                dose_seen.add(dose_text)
                doses.append(dose_text)
        sex = _sex_label(str(group.get("sex") or ""))
        n_animals = group.get("n_animals")
        n_text = _compact_number(n_animals) if n_animals is not None else ""
        if sex:
            key = (sex, n_text)
            if key not in sex_seen:
                sex_seen.add(key)
                sex_profiles.append(key)
    doses_text = ", ".join(doses)
    if control_present:
        doses_text = (
            f"{doses_text} (plus vehicle control)" if doses_text else "Vehicle control"
        )
    sex_parts: List[str] = []
    for sex, n_text in sex_profiles:
        sex_parts.append(f"{sex}, n={n_text}" if n_text else sex)
    sex_text = "; ".join(sex_parts)
    if len(sex_profiles) == 1 and sex_text:
        sex_text = f"{sex_text} (1 group)"
    return doses_text, sex_text


def _format_metric_value(value: Any) -> str:
    """Format metric value."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    normalized = number.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    compact = format(normalized, "f").rstrip("0").rstrip(".")
    return compact or "0"


def _summarize_exposure_metrics(
    rows: Sequence[Dict[str, Any]], *, max_items: int = 8
) -> List[str]:
    """Summarize exposure metrics."""
    summarized: List[str] = []
    seen: Set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        parameter = str(row.get("parameter") or "").strip()
        if not parameter:
            continue
        value = _format_metric_value(row.get("value"))
        unit = str(row.get("unit") or "").strip()
        timepoint = str(row.get("timepoint") or "").strip()
        fragments = [parameter]
        if value:
            fragments.append(value)
        if unit:
            fragments.append(unit)
        summary = " ".join(fragments).strip()
        if timepoint:
            summary = f"{summary} ({timepoint})"
        key = summary.lower()
        if not summary or key in seen:
            continue
        seen.add(key)
        summarized.append(summary)
        if len(summarized) >= max_items:
            break
    return summarized


def _summarize_findings(rows: Sequence[Dict[str, Any]], *, max_items: int = 6) -> List[str]:
    """Summarize findings."""
    summarized: List[str] = []
    seen: Set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        finding = str(row.get("finding_term") or "").strip()
        if not finding:
            continue
        organ = str(row.get("organ") or "").strip()
        organ_system = str(row.get("organ_system") or "").strip()
        severity = str(row.get("severity") or "").strip()
        prefix = _first_non_empty((organ, organ_system))
        summary = f"{prefix}: {finding}" if prefix else finding
        if severity:
            summary = f"{summary} ({severity})"
        key = summary.lower()
        if key in seen:
            continue
        seen.add(key)
        summarized.append(summary)
        if len(summarized) >= max_items:
            break
    return summarized


def _select_primary_safety_summary(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Select primary safety summary."""
    best: Dict[str, Any] = {}
    best_score = -1
    for row in rows:
        if not isinstance(row, dict):
            continue
        score = 0
        if str(row.get("noael_mg_per_kg") or "").strip():
            score += 1
        if str(row.get("loael_mg_per_kg") or "").strip():
            score += 1
        if str(row.get("limiting_organ") or "").strip():
            score += 1
        if str(row.get("limiting_finding") or "").strip():
            score += 1
        if score > best_score:
            best = row
            best_score = score
    return best


def _build_document_detail_profiles(
    context: Dict[str, Any],
    *,
    max_profiles: int = 40,
) -> List[Dict[str, Any]]:
    """Build document detail profiles."""
    payload = context.get("ncd_payload") or {}
    if not isinstance(payload, dict):
        return []

    study_records = _extract_ncd_study_records(context)
    if not study_records:
        return []

    dose_groups = payload.get("dose_groups") if isinstance(payload.get("dose_groups"), list) else []
    exposure_metrics = (
        payload.get("exposure_metrics")
        if isinstance(payload.get("exposure_metrics"), list)
        else []
    )
    findings = payload.get("findings") if isinstance(payload.get("findings"), list) else []
    safety_summaries = (
        payload.get("safety_summaries")
        if isinstance(payload.get("safety_summaries"), list)
        else []
    )

    dose_by_study: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in dose_groups:
        if not isinstance(row, dict):
            continue
        study_id = str(row.get("study_id") or "").strip()
        if study_id:
            dose_by_study[study_id].append(row)

    exposure_by_study: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in exposure_metrics:
        if not isinstance(row, dict):
            continue
        study_id = str(row.get("study_id") or "").strip()
        if study_id:
            exposure_by_study[study_id].append(row)

    findings_by_study: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in findings:
        if not isinstance(row, dict):
            continue
        study_id = str(row.get("study_id") or "").strip()
        if study_id:
            findings_by_study[study_id].append(row)

    safety_by_study: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in safety_summaries:
        if not isinstance(row, dict):
            continue
        study_id = str(row.get("study_id") or "").strip()
        if study_id:
            safety_by_study[study_id].append(row)

    profiles: List[Dict[str, Any]] = []
    for record in study_records[:max_profiles]:
        study_id = str(record.get("study_id") or "").strip()
        if not study_id:
            continue
        study_dose_groups = dose_by_study.get(study_id, [])
        study_exposure = exposure_by_study.get(study_id, [])
        study_findings = findings_by_study.get(study_id, [])
        study_safety_rows = safety_by_study.get(study_id, [])
        safety_row = _select_primary_safety_summary(study_safety_rows)

        doses_text, sex_group_text = _render_dose_group_summary(study_dose_groups)
        exposure_summary = _summarize_exposure_metrics(study_exposure)
        finding_summary = _summarize_findings(study_findings)

        noael = _format_metric_value(safety_row.get("noael_mg_per_kg"))
        loael = _format_metric_value(safety_row.get("loael_mg_per_kg"))
        limiting_finding = _first_non_empty(
            (
                safety_row.get("limiting_finding"),
                "; ".join(finding_summary[:2]),
            )
        )

        source_chunk_refs: Set[str] = set()
        for row in [*study_exposure, *study_findings, *study_safety_rows]:
            source_chunk_id = str(row.get("source_chunk_id") or "").strip()
            if source_chunk_id:
                source_chunk_refs.add(source_chunk_id)

        missing_detail_fields: List[str] = []
        if not doses_text:
            missing_detail_fields.append("dose_groups")
        if not exposure_summary:
            missing_detail_fields.append("exposure_metrics")
        if not finding_summary:
            missing_detail_fields.append("findings")
        if not (noael or loael or limiting_finding):
            missing_detail_fields.append("safety_summary")

        profiles.append(
            {
                "study_id": study_id,
                "study_number": record.get("study_number"),
                "module4_section": record.get("module4_section"),
                "type_of_study": _truncate_text(str(record.get("type_of_study") or ""), 140),
                "test_system": _truncate_text(str(record.get("test_system") or ""), 120),
                "method_of_administration": _truncate_text(
                    str(record.get("method_of_administration") or ""), 120
                ),
                "glp_compliance": record.get("glp_compliance") or "",
                "dose_summary_mg_per_kg": doses_text,
                "group_size_summary": sex_group_text,
                "exposure_metrics": exposure_summary,
                "finding_highlights": finding_summary,
                "noael_mg_per_kg": noael,
                "loael_mg_per_kg": loael,
                "limiting_finding": _truncate_text(str(limiting_finding or ""), 200),
                "location_in_ctd": _truncate_text(str(record.get("location_in_ctd") or ""), 220),
                "traceability": {
                    "dose_group_rows": len(study_dose_groups),
                    "exposure_rows": len(study_exposure),
                    "finding_rows": len(study_findings),
                    "safety_summary_rows": len(study_safety_rows),
                    "source_chunk_refs": len(source_chunk_refs),
                },
                "missing_detail_fields": missing_detail_fields,
            }
        )
    return profiles


def _merge_text_segments(*values: str, separator: str = "; ") -> str:
    """Merge text segments."""
    merged: List[str] = []
    seen: Set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        parts = [part.strip() for part in re.split(r"\s*;\s*", text) if part.strip()]
        if not parts:
            parts = [text]
        for part in parts:
            key = part.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(part)
    return separator.join(merged)


def _study_numbers_overlap(left: str, right: str) -> bool:
    """Study numbers overlap."""
    left_ids = {sid.upper() for sid in _extract_study_ids(str(left or ""))}
    right_ids = {sid.upper() for sid in _extract_study_ids(str(right or ""))}
    if left_ids and right_ids:
        return bool(left_ids & right_ids)
    left_value = str(left or "").strip().upper()
    right_value = str(right or "").strip().upper()
    return bool(left_value and right_value and left_value == right_value)


_SAFETY_NEGATIVE_FINDING_RE = re.compile(
    r"\b(no|not|without)\b.*\b(treatment-related|change|changes|adverse|"
    r"effect|effects|toxicologically meaningful|clinically meaningful)\b",
    re.IGNORECASE,
)
_SAFETY_POSITIVE_FINDING_RE = re.compile(
    r"\b(increase|increased|decrease|decreased|prolong|shorten|elevat|reduc|"
    r"finding|signal|change)\b",
    re.IGNORECASE,
)

_SAFETY_ORGAN_SYSTEM_RULES: Sequence[Tuple[str, Sequence[str]]] = (
    (
        "Cardiovascular",
        (
            r"\bcardiovascular\b",
            r"\bcardiac\b",
            r"\bhemodynamic[s]?\b",
            r"\bblood pressure\b",
            r"\bheart rate\b",
            r"\becg\b",
            r"\bqtc?\b",
            r"\bqrs\b",
            r"\bpr interval\b",
            r"\bherg\b",
            r"\btelemetry\b",
        ),
    ),
    (
        "CNS",
        (
            r"\bcns\b",
            r"\bcentral nervous",
            r"\bneuro(?:logical|behavior|behaviour)?\b",
            r"\birwin\b",
            r"\bfob\b",
            r"\bfunctional observational battery\b",
            r"\bmotor activity\b",
            r"\bconvulsion",
            r"\bseizure",
        ),
    ),
    (
        "Respiratory",
        (
            r"\brespiratory\b",
            r"\bpulmonary\b",
            r"\bplethysmography\b",
            r"\btidal volume\b",
            r"\bminute volume\b",
            r"\brespiratory rate\b",
            r"\bblood gases?\b",
        ),
    ),
    (
        "Gastrointestinal",
        (
            r"\bgastrointestinal\b",
            r"\bgi tract\b",
        ),
    ),
    (
        "Renal",
        (
            r"\brenal\b",
            r"\bkidney\b",
        ),
    ),
)


def _normalize_glp_value(value: Any) -> str:
    """Normalize glp value."""
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if re.search(r"\b(non[\s-]*glp|not[\s-]*glp|noncompliant)\b", lowered):
        return "Non-GLP"
    if re.search(r"\bglp\b", lowered):
        return "GLP"
    compact = re.sub(r"[^a-z0-9]+", "", lowered)
    if compact in {"yes", "y", "true", "compliant"}:
        return "GLP"
    if compact in {"no", "n", "false", "na", "n/a", "unknown"}:
        return ""
    return text


def _infer_glp_compliance(*values: Any) -> str:
    """Infer glp compliance."""
    for value in values:
        normalized = _normalize_glp_value(value)
        if normalized in {"GLP", "Non-GLP"}:
            return normalized
    return ""


def _extract_location_title(value: Any) -> str:
    """Extract location title."""
    def _normalize(value: str) -> str:
        """Normalize."""
        return re.sub(r"\s+", " ", str(value or "")).strip()

    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r'"([^"]+)"', text)
    if match:
        return _normalize(match.group(1))
    return ""


def _asset_single_study_id(asset: Dict[str, Any]) -> str:
    """Asset single study id."""
    asset_blob = " ".join(
        str(part)
        for part in (
            asset.get("s3_key"),
            asset.get("json_key"),
            asset.get("caption"),
            asset.get("description"),
        )
        if part
    )
    asset_study_candidates: List[str] = []
    for token in _extract_study_ids(asset_blob):
        cleaned = re.sub(
            r"\.pdf(?:\.(?:tables|images))?(?:/.*)?$",
            "",
            str(token),
            flags=re.IGNORECASE,
        )
        cleaned = cleaned.strip("._-")
        canonical = _canonical_study_number(cleaned)
        if canonical and "/" not in canonical:
            asset_study_candidates.append(canonical)
    deduped_asset_ids: List[str] = []
    seen_asset_ids: Set[str] = set()
    for candidate in asset_study_candidates:
        key = candidate.upper()
        if key in seen_asset_ids:
            continue
        seen_asset_ids.add(key)
        deduped_asset_ids.append(candidate)
    return deduped_asset_ids[0] if len(deduped_asset_ids) == 1 else ""


_PRIMARY_PD_ROUTE_PATTERNS: Sequence[Tuple[str, Sequence[str]]] = (
    ("In vitro", (r"\bin vitro\b", r"\bcell[- ]based\b", r"\belisa\b")),
    ("Intravenous", (r"\bintravenous\b", r"\biv\b")),
    ("Subcutaneous", (r"\bsubcutaneous\b", r"\bsc\b")),
    ("Intraperitoneal", (r"\bintraperitoneal\b", r"\bip\b")),
    ("Intrathecal", (r"\bintrathecal\b", r"\bit\b")),
    ("Intraplantar", (r"\bintraplantar\b",)),
    ("Oral", (r"\boral\b", r"\bpo\b")),
    ("Topical", (r"\btopical\b",)),
    ("Inhalation", (r"\binhal(?:ation|ed)\b",)),
)

_PRIMARY_PD_STRAIN_RULES: Sequence[Tuple[str, str, Sequence[str]]] = (
    (
        "Sprague-Dawley",
        "Rat",
        (
            r"\bsprague[- ]?dawley\b",
            r"\bsd rats?\b",
        ),
    ),
    (
        "Wistar",
        "Rat",
        (
            r"\bwistar\b",
            r"\bwistar[- ]rats?\b",
        ),
    ),
    (
        "C57BL/6",
        "Mouse",
        (
            r"\bc57\s*bl\s*/?\s*6\b",
            r"\bc57bl/?6\b",
        ),
    ),
    ("DBA/2", "Mouse", (r"\bdba/?2\b",)),
    ("CBA", "Mouse", (r"\bcba\b",)),
    ("NMRI", "Mouse", (r"\bnmri\b",)),
    ("CD-1", "Mouse", (r"\bcd[- ]?1\b",)),
    ("ZDF", "Rat", (r"\bzdf\b", r"\bzucker diabetic fatty\b")),
)

_PRIMARY_PD_SPECIES_RULES: Sequence[Tuple[str, Sequence[str]]] = (
    ("Cynomolgus monkey", (r"\bcynomolgus\b",)),
    ("Monkey", (r"\bmonkeys?\b", r"\bnon[- ]human primates?\b")),
    ("Dog", (r"\bdogs?\b", r"\bbeagles?\b")),
    ("Rabbit", (r"\brabbits?\b",)),
    ("Rat", (r"\brats?\b",)),
    ("Mouse", (r"\bmice\b", r"\bmouse\b")),
    ("Human whole blood (in vitro)", (r"\bhuman whole blood\b",)),
)

_PRIMARY_PD_DOSE_SERIES_RE = re.compile(
    r"\b((?:\d+(?:\.\d+)?\s*(?:,|and|or)\s*)+\d+(?:\.\d+)?)\s*"
    r"(mg|ug|g)\s*/\s*kg\b",
    re.IGNORECASE,
)
_PRIMARY_PD_DOSE_SINGLE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(mg|ug|g)\s*/\s*kg\b",
    re.IGNORECASE,
)
_PRIMARY_PD_ABSOLUTE_DOSE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(ug|mg)\b\s*(?:intrathecal|it)\b",
    re.IGNORECASE,
)
_PRIMARY_PD_GROUP_PATTERNS: Sequence[re.Pattern[str]] = (
    re.compile(r"\bn\s*=\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\b(\d+)\s*(?:animals?|rats?|mice|monkeys?|dogs?)\s*/\s*group\b", re.IGNORECASE),
    re.compile(r"\b(\d+)\s*per\s*group\b", re.IGNORECASE),
)


def _infer_method_of_administration(*values: Any) -> str:
    """Infer administration route from free text."""
    text = " ".join(str(value or "") for value in values if str(value or "").strip())
    if not text:
        return ""
    lowered = text.lower()
    routes: List[str] = []
    for label, patterns in _PRIMARY_PD_ROUTE_PATTERNS:
        if any(re.search(pattern, lowered) for pattern in patterns):
            routes.append(label)
    seen: Set[str] = set()
    ordered: List[str] = []
    for route in routes:
        key = route.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(route)
    return "; ".join(ordered)


def _infer_species_strain_from_text(*values: Any) -> str:
    """Infer species/strain from free text."""
    text = " ".join(str(value or "") for value in values if str(value or "").strip())
    if not text:
        return ""
    lowered = text.lower()
    strains: List[str] = []
    inferred_species = ""
    for strain, species, patterns in _PRIMARY_PD_STRAIN_RULES:
        if any(re.search(pattern, lowered) for pattern in patterns):
            strains.append(strain)
            if not inferred_species:
                inferred_species = species
    species = inferred_species
    for label, patterns in _PRIMARY_PD_SPECIES_RULES:
        if any(re.search(pattern, lowered) for pattern in patterns):
            species = label
            break
    if not species:
        return ""
    if strains:
        unique_strains: List[str] = []
        seen: Set[str] = set()
        for strain in strains:
            key = strain.lower()
            if key in seen:
                continue
            seen.add(key)
            unique_strains.append(strain)
        return f"{species} ({', '.join(unique_strains)})"
    return species


def _infer_dose_summary_from_text(*values: Any) -> str:
    """Infer dose summary from free text."""
    text = " ".join(str(value or "") for value in values if str(value or "").strip())
    if not text:
        return ""
    doses: List[str] = []
    seen: Set[str] = set()

    def _add_dose(number_text: str, unit: str, *, per_kg: bool = True) -> None:
        compact = _format_metric_value(number_text)
        if not compact:
            return
        normalized_unit = unit.lower()
        suffix = f"{normalized_unit}/kg" if per_kg else normalized_unit
        dose = f"{compact} {suffix}"
        key = dose.lower()
        if key in seen:
            return
        seen.add(key)
        doses.append(dose)

    for match in _PRIMARY_PD_DOSE_SERIES_RE.finditer(text):
        number_series = str(match.group(1) or "")
        unit = str(match.group(2) or "")
        for part in re.split(r"\s*(?:,|and|or)\s*", number_series):
            if not part:
                continue
            _add_dose(part, unit, per_kg=True)

    for number_text, unit in _PRIMARY_PD_DOSE_SINGLE_RE.findall(text):
        _add_dose(number_text, unit, per_kg=True)

    for number_text, unit in _PRIMARY_PD_ABSOLUTE_DOSE_RE.findall(text):
        _add_dose(number_text, unit, per_kg=False)

    return ", ".join(doses)


def _infer_group_size_from_text(*values: Any) -> str:
    """Infer sex and n/group from free text."""
    text = " ".join(str(value or "") for value in values if str(value or "").strip())
    if not text:
        return ""
    lowered = text.lower()
    sex_label = ""
    has_male = bool(re.search(r"\bmale\b", lowered))
    has_female = bool(re.search(r"\bfemale\b", lowered))
    if has_male and has_female:
        sex_label = "Male/Female"
    elif has_male:
        sex_label = "Male"
    elif has_female:
        sex_label = "Female"

    group_sizes: List[str] = []
    seen_sizes: Set[str] = set()
    for pattern in _PRIMARY_PD_GROUP_PATTERNS:
        for match in pattern.findall(text):
            size = str(match or "").strip()
            if not size or size in seen_sizes:
                continue
            seen_sizes.add(size)
            group_sizes.append(size)

    if sex_label and len(group_sizes) == 1:
        return f"{sex_label}, n={group_sizes[0]}/group"
    if sex_label and group_sizes:
        joined = ", ".join(f"n={size}/group" for size in group_sizes[:3])
        return f"{sex_label}; {joined}"
    if group_sizes:
        return ", ".join(f"n={size}/group" for size in group_sizes[:3])
    if sex_label:
        return sex_label
    return ""


def _infer_primary_pd_endpoints(*values: Any) -> str:
    """Infer primary pd endpoints."""
    text = " ".join(str(value or "") for value in values if str(value or "").strip())
    if not text:
        return ""
    lowered = text.lower()
    labels: List[str] = []
    if any(token in lowered for token in ("tumor", "xenograft", "allograft", "metast")):
        labels.append("Tumor growth/volume")
    if any(token in lowered for token in ("angiogenesis", "vascular", "matrigel")):
        labels.append("Angiogenesis-related endpoints")
    if any(token in lowered for token in ("survival", "mortality")):
        labels.append("Survival")
    if "body weight" in lowered:
        labels.append("Body weight")
    if any(token in lowered for token in ("pain", "nocicept", "analgesi")):
        labels.append("Pain response endpoints")
    if labels:
        return "; ".join(labels)
    return "Primary pharmacodynamic efficacy endpoints"


def _infer_primary_pd_findings(*values: Any) -> str:
    """Infer primary pd findings."""
    def _normalize(value: str) -> str:
        """Normalize."""
        return re.sub(r"\s+", " ", str(value or "")).strip()

    text = " ".join(str(value or "") for value in values if str(value or "").strip())
    if not text:
        return ""
    for sentence in _extract_safety_sentences(text, max_parts=6):
        lowered = sentence.lower()
        if any(
            token in lowered
            for token in (
                "inhibit",
                "mitigat",
                "reduc",
                "suppress",
                "prevent",
                "attenuat",
                "improv",
                "increase",
                "decrease",
            )
        ):
            return _truncate_text(_normalize(sentence), max_chars=320)
    first = _extract_safety_sentences(text, max_parts=1)
    if first:
        return _truncate_text(_normalize(first[0]), max_chars=320)
    return _truncate_text(_normalize(text), max_chars=320)


def _looks_like_safety_pharmacology_text(*values: Any) -> bool:
    """Looks like safety pharmacology text."""
    text = " ".join(str(value or "") for value in values if str(value or "").strip())
    if not text:
        return False
    lowered = text.lower()
    if "safety pharmacology" in lowered:
        return True
    for _label, patterns in _SAFETY_ORGAN_SYSTEM_RULES:
        if any(re.search(pattern, lowered) for pattern in patterns):
            return True
    return False


def _infer_safety_organ_systems(*values: Any) -> str:
    """Infer safety organ systems."""
    text = " ".join(str(value or "") for value in values if str(value or "").strip())
    if not text:
        return ""
    lowered = text.lower()
    inferred: List[str] = []
    for label, patterns in _SAFETY_ORGAN_SYSTEM_RULES:
        if any(re.search(pattern, lowered) for pattern in patterns):
            inferred.append(label)
    return "; ".join(inferred)


def _asset_matches_safety_pharmacology(asset: Dict[str, Any]) -> bool:
    """Asset matches safety pharmacology."""
    if not isinstance(asset, dict):
        return False
    extra = (
        asset.get("extra_attributes") if isinstance(asset.get("extra_attributes"), dict) else {}
    )
    parts: List[str] = [
        str(asset.get("s3_key") or ""),
        str(asset.get("json_key") or ""),
        str(asset.get("caption") or ""),
        str(asset.get("description") or ""),
        str(extra.get("section_number") or ""),
        str(extra.get("module4_section") or ""),
        str(extra.get("section_title") or ""),
    ]
    keywords = asset.get("keywords") or []
    if isinstance(keywords, list):
        parts.extend(str(item) for item in keywords if item)
    blob = " ".join(part for part in parts if part).strip()
    if not blob:
        return False
    if _gap_key_mentions_module4_section(blob, "4.2.1.3"):
        return True
    return "safety pharmacology" in blob.lower()


def _asset_matches_primary_pharmacology(asset: Dict[str, Any]) -> bool:
    """Asset matches primary pharmacology content (Module 4.2.1.1)."""
    if not isinstance(asset, dict):
        return False
    extra = (
        asset.get("extra_attributes") if isinstance(asset.get("extra_attributes"), dict) else {}
    )
    parts: List[str] = [
        str(asset.get("s3_key") or ""),
        str(asset.get("json_key") or ""),
        str(asset.get("caption") or ""),
        str(asset.get("description") or ""),
        str(extra.get("section_number") or ""),
        str(extra.get("module4_section") or ""),
        str(extra.get("section_title") or ""),
    ]
    keywords = asset.get("keywords") or []
    if isinstance(keywords, list):
        parts.extend(str(item) for item in keywords if item)
    blob = " ".join(part for part in parts if part).strip()
    if not blob:
        return False
    lowered = blob.lower()
    if _gap_key_mentions_module4_section(blob, "4.2.1.1"):
        return True
    return "primary pharmacodynamics" in lowered or "primary pharmacology" in lowered


def _extract_safety_sentences(text: str, max_parts: int = 4) -> List[str]:
    """Extract safety sentences."""
    parts: List[str] = []
    source_text = _clean_extracted_text(text)
    if not source_text:
        return parts
    for chunk in re.split(r"(?<=[.!?])\s+|\s*;\s*", source_text):
        cleaned = _clean_extracted_text(chunk)
        if not cleaned:
            continue
        if _is_noisy_extraction_fragment(cleaned):
            continue
        if sum(1 for ch in cleaned if ch.isalpha()) < 5:
            continue
        parts.append(cleaned)
        if len(parts) >= max_parts:
            break
    return parts


def _synthesize_safety_findings(fragments: Sequence[str]) -> str:
    """Synthesize safety findings."""
    negatives: List[str] = []
    positives: List[str] = []
    neutral: List[str] = []
    seen: Set[str] = set()
    for fragment in fragments:
        for sentence in _extract_safety_sentences(fragment):
            key = sentence.lower()
            if key in seen:
                continue
            seen.add(key)
            if _SAFETY_NEGATIVE_FINDING_RE.search(sentence):
                negatives.append(sentence)
            elif _SAFETY_POSITIVE_FINDING_RE.search(sentence):
                positives.append(sentence)
            else:
                neutral.append(sentence)
    selected: List[str] = []
    if negatives:
        selected.append(negatives[0])
    if positives:
        selected.extend(positives[:2])
    if not selected:
        selected.extend(neutral[:2])
    if not selected:
        return ""
    return _truncate_text("; ".join(selected), max_chars=900)


def _collect_safety_context_fragments(
    context: Dict[str, Any],
    *,
    study_number: str,
) -> List[str]:
    """Collect safety context fragments."""
    fragments: List[str] = []
    for source in context.get("section_sources", []) or []:
        if not isinstance(source, dict):
            continue
        section_number = str(source.get("section_number") or "").strip()
        if section_number and not section_number_matches(section_number, "4.2.1.3"):
            continue
        summary_text = str(source.get("summary_text") or "").strip()
        if not summary_text:
            continue
        source_blob = " ".join(
            str(part)
            for part in (
                source.get("section_title"),
                source.get("section_number"),
                source.get("s3_key"),
                summary_text,
            )
            if part
        )
        source_ids = _extract_study_ids(source_blob)
        if source_ids and not any(
            _study_numbers_overlap(sid, study_number) for sid in source_ids
        ):
            continue
        fragments.extend(_extract_safety_sentences(summary_text, max_parts=3))
    for asset in context.get("table_assets", []) or []:
        preview_rows = asset.get("preview_rows") or []
        for row in preview_rows:
            if not isinstance(row, dict):
                continue
            key_map = {_normalize_header_token(str(key)): key for key in row.keys()}
            row_study = _pick_first_value(row, key_map, "study number")
            if row_study and not _study_numbers_overlap(row_study, study_number):
                continue
            snippet = _pick_first_value(
                row,
                key_map,
                "noteworthy findings",
                "key findings",
                "key results",
                "findings",
            )
            if snippet:
                fragments.extend(_extract_safety_sentences(snippet, max_parts=2))
    return fragments


def _format_study_number_display(study_number: str, extra: Dict[str, Any]) -> str:
    """Format study number display."""
    display = str(extra.get("study_number_display") or "").strip()
    if display:
        return display
    sponsor = str(extra.get("sponsor_study_number") or "").strip()
    cro = str(extra.get("cro_study_number") or "").strip()
    combined = _merge_text_segments(
        f"Sponsor study #: {sponsor}" if sponsor else "",
        f"CRO study #: {cro}" if cro else "",
    )
    if combined:
        return combined
    if study_number and cro and study_number.lower() != cro.lower():
        return _merge_text_segments(study_number, f"CRO study #: {cro}")
    return study_number


def _merge_safety_candidate_records(
    candidates: Sequence[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Merge safety candidate records."""
    merged_by_study: Dict[str, Dict[str, str]] = {}
    passthrough: List[Dict[str, str]] = []
    for candidate in candidates:
        normalized = {str(key): str(value or "").strip() for key, value in candidate.items()}
        study_number = normalized.get("study number", "")
        canonical = _canonical_study_number(study_number)
        study_key = canonical.upper() if canonical else study_number.upper()
        if not study_key:
            passthrough.append(normalized)
            continue
        existing = merged_by_study.get(study_key)
        if existing is None:
            merged_by_study[study_key] = normalized
            continue
        for field, value in normalized.items():
            if not value:
                continue
            current = str(existing.get(field) or "").strip()
            if not current:
                existing[field] = value
                continue
            token = _normalize_header_token(field)
            if (
                token in {"study number", "organ systems evaluated", "noteworthy findings"}
                and current.lower() != value.lower()
            ):
                existing[field] = _merge_text_segments(current, value)
    ordered = sorted(
        merged_by_study.values(),
        key=lambda row: _study_sort_key(str(row.get("study number") or "")),
    )
    return [*ordered, *passthrough]


def _row_non_study_quality_score(
    row: Dict[str, Any],
    columns: Sequence[str],
    *,
    study_col: Optional[str],
) -> int:
    """Score row quality by counting meaningful non-study values."""
    score = 0
    for col in columns:
        if study_col and col == study_col:
            continue
        text = str(row.get(col) or "").strip()
        if not text:
            continue
        token = _normalize_header_token(col)
        if token == "glp compliance" and text.lower() == "not reported":
            continue
        if _is_placeholder_value(text):
            continue
        score += 1
    return score


def _rows_from_token_candidates(
    columns: Sequence[str], candidates: Sequence[Dict[str, str]]
) -> List[Dict[str, str]]:
    """Rows from token candidates."""
    passthrough_rows: List[Dict[str, str]] = []
    study_col = next(
        (col for col in columns if _normalize_header_token(col) == "study number"), None
    )
    deduped: Dict[str, Dict[str, Any]] = {}
    for candidate in candidates:
        row = {str(col): "" for col in columns}
        for col in columns:
            token = _normalize_header_token(str(col))
            value = str(candidate.get(token) or "").strip()
            if value:
                row[str(col)] = value
        if not any(str(value).strip() for value in row.values()):
            continue
        quality_score = _row_non_study_quality_score(
            row,
            columns,
            study_col=study_col,
        )
        if quality_score == 0:
            continue
        if not study_col:
            passthrough_rows.append(row)
            continue
        parsed_ids = _extract_study_ids(str(row.get(study_col) or ""))
        if not parsed_ids:
            passthrough_rows.append(row)
            continue
        canonical_id = parsed_ids[0]
        row[study_col] = canonical_id
        score = quality_score
        current = deduped.get(canonical_id)
        if current is None or score > int(current.get("_score", 0)):
            row_with_score = dict(row)
            row_with_score["_score"] = score
            deduped[canonical_id] = row_with_score
    if deduped:
        ordered_ids = sorted(deduped.keys(), key=_study_sort_key)
        normalized_rows = [
            {k: v for k, v in deduped[study_id].items() if k != "_score"}
            for study_id in ordered_ids
        ]
        return [*normalized_rows, *passthrough_rows]
    return passthrough_rows


def _extract_primary_pd_candidates(context: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract primary pd candidates."""
    candidates: List[Dict[str, str]] = []
    study_id_candidates = _build_study_id_candidates(context)
    ncd_records = _extract_ncd_study_records(context, module4_section="4.2.1.1")
    if ncd_records:
        payload = context.get("ncd_payload") or {}
        dose_groups = payload.get("dose_groups") if isinstance(payload, dict) else []
        dose_groups_by_study: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        if isinstance(dose_groups, list):
            for group in dose_groups:
                if not isinstance(group, dict):
                    continue
                study_id = str(group.get("study_id") or "").strip()
                if study_id:
                    dose_groups_by_study[study_id].append(group)
        for record in ncd_records:
            extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
            study_id = str(record.get("study_id") or "").strip()
            study_dose_groups = dose_groups_by_study.get(study_id, [])
            dose_text, sex_group_text = _render_dose_group_summary(study_dose_groups)
            study_number = str(record.get("study_number") or "").strip()
            location_in_ctd = str(record.get("location_in_ctd") or "")
            location_title = _extract_location_title(location_in_ctd)
            context_fragments: List[str] = []
            for source in context.get("section_sources", []) or []:
                if not isinstance(source, dict):
                    continue
                section_number = str(source.get("section_number") or "").strip()
                summary_text = _clean_extracted_text(source.get("summary_text"))
                if not summary_text:
                    continue
                source_blob = " ".join(
                    str(part)
                    for part in (
                        source.get("section_title"),
                        source.get("section_number"),
                        source.get("s3_key"),
                        summary_text,
                    )
                    if part
                )
                if section_number:
                    if not section_number_matches(section_number, "4.2.1.1"):
                        continue
                elif not (
                    _gap_key_mentions_module4_section(source_blob, "4.2.1.1")
                    or "primary pharmacodynamics" in source_blob.lower()
                    or "primary pharmacology" in source_blob.lower()
                ):
                    continue
                source_ids = _extract_study_ids(source_blob)
                if source_ids:
                    if study_number and not any(
                        _study_numbers_overlap(sid, study_number) for sid in source_ids
                    ):
                        continue
                else:
                    title_tokens = {
                        token for token in _tokenize_text(location_title) if len(token) > 3
                    }
                    source_tokens = _tokenize_text(source_blob)
                    if not title_tokens:
                        continue
                    if len(title_tokens & source_tokens) < min(3, len(title_tokens)):
                        continue
                context_fragments.extend(_extract_safety_sentences(summary_text, max_parts=4))
            context_blob = "; ".join(context_fragments)
            glp_text = _first_non_empty(
                (
                    str(record.get("glp_compliance") or ""),
                    _infer_glp_compliance(
                        extra.get("glp_compliance") if extra else "",
                        extra.get("glp_status") if extra else "",
                        extra.get("glp") if extra else "",
                        extra.get("qa_statement") if extra else "",
                        extra.get("glp_statement") if extra else "",
                        extra.get("study_title") if extra else "",
                        extra.get("title") if extra else "",
                        extra.get("key_findings") if extra else "",
                        extra.get("noteworthy_findings") if extra else "",
                        extra.get("findings") if extra else "",
                        location_title,
                        context_blob,
                    ),
                )
            )
            endpoints_text = _first_non_empty(
                (
                    extra.get("endpoints_assays") if extra else "",
                    extra.get("endpoints") if extra else "",
                    extra.get("assays") if extra else "",
                    _infer_primary_pd_endpoints(
                        extra.get("study_title") if extra else "",
                        extra.get("title") if extra else "",
                        location_title,
                        context_blob,
                    ),
                )
            )
            endpoints_text = _clean_extracted_text(endpoints_text)
            findings_text = _first_non_empty(
                (
                    extra.get("key_findings") if extra else "",
                    extra.get("noteworthy_findings") if extra else "",
                    extra.get("findings") if extra else "",
                    extra.get("result_summary") if extra else "",
                    _infer_primary_pd_findings(
                        context_blob,
                        location_title,
                        endpoints_text,
                    ),
                )
            )
            findings_text = _clean_extracted_text(findings_text)
            if not glp_text:
                glp_text = "Not reported"
            type_of_study = _first_non_empty(
                (
                    extra.get("type_of_study") if extra else "",
                    record.get("type_of_study"),
                    extra.get("study_title") if extra else "",
                    extra.get("title") if extra else "",
                )
            )
            type_of_study = _clean_extracted_text(type_of_study)
            test_system_value = _first_non_empty(
                (
                    record.get("test_system"),
                    f"{record.get('species')}; {record.get('strain')}"
                    if record.get("species") and record.get("strain")
                    else "",
                    record.get("species"),
                )
            )
            test_system_value = _clean_extracted_text(test_system_value)
            if _is_placeholder_value(test_system_value):
                test_system_value = ""
            if not test_system_value:
                test_system_value = _infer_species_strain_from_text(
                    extra.get("study_title") if extra else "",
                    extra.get("title") if extra else "",
                    location_title,
                    context_blob,
                )
            method_value = _clean_extracted_text(record.get("method_of_administration"))
            if _is_placeholder_value(method_value):
                method_value = ""
            if not method_value:
                method_value = _infer_method_of_administration(
                    extra.get("method_of_administration") if extra else "",
                    extra.get("route_of_administration") if extra else "",
                    extra.get("route") if extra else "",
                    extra.get("study_title") if extra else "",
                    extra.get("title") if extra else "",
                    location_title,
                    context_blob,
                )
            dose_value = _first_non_empty(
                (
                    extra.get("dose_concentration") if extra else "",
                    extra.get("dose_levels") if extra else "",
                    extra.get("dose_level") if extra else "",
                    extra.get("dose") if extra else "",
                    dose_text,
                )
            )
            dose_value = _clean_extracted_text(dose_value)
            if _is_placeholder_value(dose_value):
                dose_value = ""
            if not dose_value:
                dose_value = _infer_dose_summary_from_text(
                    extra.get("dose_concentration") if extra else "",
                    extra.get("dose_levels") if extra else "",
                    extra.get("dose_level") if extra else "",
                    extra.get("dose") if extra else "",
                    extra.get("study_title") if extra else "",
                    extra.get("title") if extra else "",
                    location_title,
                    context_blob,
                )
            group_value = _first_non_empty(
                (
                    extra.get("gender_group") if extra else "",
                    extra.get("sex_and_n_per_group") if extra else "",
                    sex_group_text,
                )
            )
            group_value = _clean_extracted_text(group_value)
            if _is_placeholder_value(group_value):
                group_value = ""
            if not group_value:
                group_value = _infer_group_size_from_text(
                    extra.get("gender_group") if extra else "",
                    extra.get("sex_and_n_per_group") if extra else "",
                    extra.get("study_title") if extra else "",
                    extra.get("title") if extra else "",
                    location_title,
                    context_blob,
                )
            location_in_ctd = _clean_extracted_text(location_in_ctd)
            candidates.append(
                {
                    "study number": study_number,
                    "type of study": type_of_study,
                    "species strain or test system": test_system_value,
                    "species strain": test_system_value,
                    "method of administration": method_value,
                    "method of admin": method_value,
                    "dose concentration": dose_value,
                    "doses": dose_value,
                    "gender and no per group": group_value,
                    "endpoints assays": endpoints_text,
                    "noteworthy findings": findings_text,
                    "glp compliance": glp_text,
                    "location in ctd": location_in_ctd,
                }
            )
    seen: set[str] = set()
    candidate_by_study: Dict[str, Dict[str, str]] = {}
    for candidate in candidates:
        raw_study = str(candidate.get("study number") or "").strip()
        canonical = _canonical_study_number(raw_study)
        key = (canonical or raw_study).upper()
        if not key:
            continue
        seen.add(key)
        candidate_by_study[key] = candidate

    for asset in context.get("table_assets", []) or []:
        if not _asset_matches_primary_pharmacology(asset):
            continue
        asset_blob = " ".join(
            str(part)
            for part in (
                asset.get("s3_key"),
                asset.get("json_key"),
                asset.get("caption"),
                asset.get("description"),
            )
            if part
        )
        asset_study_id = _asset_single_study_id(asset)
        preview_rows = asset.get("preview_rows") or []
        for row in preview_rows:
            if not isinstance(row, dict):
                continue
            key_map = {_normalize_header_token(str(key)): key for key in row.keys()}
            study_number = _pick_first_value(row, key_map, "study number")
            if study_number:
                parsed_ids = _extract_study_ids(study_number)
                canonical_id = parsed_ids[0] if parsed_ids else study_number.strip()
            else:
                canonical_id = asset_study_id
            if not canonical_id:
                continue
            normalized_id = _select_study_id_from_text(canonical_id, study_id_candidates)
            if normalized_id:
                canonical_id = normalized_id
            row_blob = " ".join(
                f"{str(key)}: {_clean_extracted_text(value)}"
                for key, value in row.items()
                if _clean_extracted_text(value)
            )
            if not row_blob or _is_noisy_extraction_fragment(row_blob):
                continue
            type_text = _pick_first_value(
                row,
                key_map,
                "type of study",
                "study title",
                "study description",
                "title",
            )
            species_text = _pick_first_value(
                row,
                key_map,
                "species strain",
                "test system",
                "species strain or test system",
            )
            method_text = _pick_first_value(row, key_map, "method of administration")
            dose_text = _pick_first_value(row, key_map, "dose concentration", "doses")
            gender_text = _pick_first_value(
                row,
                key_map,
                "gender and no per group",
                "gender",
                "sex",
            )
            endpoints_text = _pick_first_value(
                row,
                key_map,
                "endpoints assays",
                "endpoints",
                "assays",
            )
            findings_text = _pick_first_value(
                row,
                key_map,
                "noteworthy findings",
                "key findings",
                "key results",
                "findings",
                "result summary",
            )
            type_text = _clean_extracted_text(type_text)
            species_text = _clean_extracted_text(species_text)
            method_text = _clean_extracted_text(method_text)
            dose_text = _clean_extracted_text(dose_text)
            gender_text = _clean_extracted_text(gender_text)
            endpoints_text = _clean_extracted_text(endpoints_text)
            findings_text = _clean_extracted_text(findings_text)
            if not endpoints_text:
                endpoints_text = _infer_primary_pd_endpoints(row_blob, asset_blob)
            if not findings_text:
                findings_text = _infer_primary_pd_findings(row_blob, asset_blob, endpoints_text)
            findings_text = _clean_extracted_text(findings_text)
            endpoints_text = _clean_extracted_text(endpoints_text)
            glp_text = _first_non_empty(
                (
                    _normalize_glp_value(_pick_first_value(row, key_map, "glp compliance")),
                    _infer_glp_compliance(row_blob),
                )
            )
            if not glp_text:
                glp_text = "Not reported"
            location_text = _pick_first_value(row, key_map, "location in ctd")
            location_text = _clean_extracted_text(location_text)
            if not any(
                text
                for text in (
                    type_text,
                    species_text,
                    method_text,
                    dose_text,
                    endpoints_text,
                    findings_text,
                    glp_text,
                    location_text,
                    gender_text,
                )
            ):
                continue
            doses_key = key_map.get("doses")
            if not dose_text and doses_key:
                dose_text = str(row.get(doses_key or "") or "").strip()
            asset_candidate = {
                "study number": canonical_id,
                "type of study": type_text,
                "species strain or test system": species_text,
                "species strain": species_text,
                "method of administration": method_text,
                "method of admin": method_text,
                "dose concentration": dose_text,
                "doses": dose_text,
                "gender and no per group": gender_text,
                "endpoints assays": endpoints_text,
                "noteworthy findings": findings_text,
                "glp compliance": glp_text,
                "location in ctd": location_text,
            }
            key = canonical_id.upper()
            existing = candidate_by_study.get(key)
            if existing is None:
                if type_text and not asset_candidate.get("type of study"):
                    asset_candidate["type of study"] = type_text
                candidates.append(asset_candidate)
                candidate_by_study[key] = asset_candidate
                seen.add(key)
                continue
            for field, value in asset_candidate.items():
                if field == "study number":
                    continue
                text = str(value or "").strip()
                if not text:
                    continue
                current = str(existing.get(field) or "").strip()
                if not current or _is_placeholder_value(current):
                    existing[field] = text
                    continue
                if field in {"dose concentration", "endpoints assays", "noteworthy findings"}:
                    if current.lower() != text.lower():
                        existing[field] = _merge_text_segments(current, text)
                elif field == "type of study":
                    if current.lower() != text.lower():
                        existing[field] = _merge_text_segments(current, text)
                elif field == "glp compliance":
                    if current not in {"GLP", "Non-GLP"}:
                        existing[field] = text
    if seen:
        candidates.sort(
            key=lambda row: _study_sort_key(str(row.get("study number") or ""))
        )
    return candidates


def _repair_primary_pharmacodynamics_table(
    tables: Sequence[Dict[str, Any]],
    context: Dict[str, Any],
) -> None:
    """Repair primary pharmacodynamics table."""
    target = next(
        (table for table in tables if str(table.get("subsection") or "") == "2.6.3.2"),
        None,
    )
    if not target:
        return
    candidates = _extract_primary_pd_candidates(context)
    if not candidates:
        return
    columns = [str(col) for col in (target.get("columns") or []) if str(col).strip()]
    if not columns:
        columns = [
            "Study Number",
            "Species/Strain or Test System",
            "Method of Administration",
            "Dose/Concentration",
            "Endpoints/Assays",
            "Key Findings",
            "GLP Compliance",
            "Location in CTD",
        ]
        target["columns"] = columns
    target["rows"] = _rows_from_token_candidates(columns, candidates)


def _extract_safety_pharmacology_candidates(
    context: Dict[str, Any]
) -> List[Dict[str, str]]:
    """Extract safety pharmacology candidates."""
    candidates: List[Dict[str, str]] = []
    study_id_candidates = _build_study_id_candidates(context)
    ncd_records = _extract_ncd_study_records(context, module4_section="4.2.1.3")
    if ncd_records:
        payload = context.get("ncd_payload") or {}
        dose_groups = payload.get("dose_groups") if isinstance(payload, dict) else []
        findings = payload.get("findings") if isinstance(payload, dict) else []
        safety_summaries = (
            payload.get("safety_summaries") if isinstance(payload, dict) else []
        )
        findings_by_study: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        summaries_by_study: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        dose_groups_by_study: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        if isinstance(findings, list):
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                study_id = str(finding.get("study_id") or "").strip()
                if study_id:
                    findings_by_study[study_id].append(finding)
        if isinstance(safety_summaries, list):
            for summary in safety_summaries:
                if not isinstance(summary, dict):
                    continue
                study_id = str(summary.get("study_id") or "").strip()
                if study_id:
                    summaries_by_study[study_id].append(summary)
        if isinstance(dose_groups, list):
            for group in dose_groups:
                if not isinstance(group, dict):
                    continue
                study_id = str(group.get("study_id") or "").strip()
                if study_id:
                    dose_groups_by_study[study_id].append(group)
        for record in ncd_records:
            extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
            study_id = str(record.get("study_id") or "").strip()
            study_findings = findings_by_study.get(study_id, [])
            study_summaries = summaries_by_study.get(study_id, [])
            study_dose_groups = dose_groups_by_study.get(study_id, [])
            study_number = str(record.get("study_number") or "").strip()
            organ_systems: List[str] = []
            for finding in study_findings:
                organ_system = str(finding.get("organ_system") or "").strip()
                if organ_system and organ_system not in organ_systems:
                    organ_systems.append(organ_system)
            doses_text, sex_group_text = _render_dose_group_summary(study_dose_groups)
            summary_findings = [
                str(summary.get("limiting_finding") or "").strip()
                for summary in study_summaries
                if isinstance(summary, dict)
                and str(summary.get("limiting_finding") or "").strip()
            ]
            finding_terms = [
                str(finding.get("finding_term") or "").strip()
                for finding in study_findings
                if isinstance(finding, dict)
                and str(finding.get("finding_term") or "").strip()
            ][:3]
            finding_excerpts = [
                str(finding.get("excerpt") or "").strip()
                for finding in study_findings
                if isinstance(finding, dict) and str(finding.get("excerpt") or "").strip()
            ][:2]
            narrative_fragments = [
                str(extra.get("noteworthy_findings") or "").strip() if extra else "",
                str(extra.get("key_findings") or "").strip() if extra else "",
                str(extra.get("findings") or "").strip() if extra else "",
                str(extra.get("result_summary") or "").strip() if extra else "",
                "; ".join(summary_findings),
                "; ".join(finding_terms),
                "; ".join(finding_excerpts),
                *(
                    _collect_safety_context_fragments(
                        context,
                        study_number=study_number,
                    )
                    if study_number
                    else []
                ),
            ]
            findings_text = _first_non_empty(
                (
                    _synthesize_safety_findings(narrative_fragments),
                    _merge_text_segments(
                        str(extra.get("noteworthy_findings") or "") if extra else "",
                        str(extra.get("key_findings") or "") if extra else "",
                        str(extra.get("findings") or "") if extra else "",
                    ),
                    "; ".join(summary_findings),
                    "; ".join(finding_terms),
                )
            )
            organ_system_text = _first_non_empty(
                (
                    extra.get("organ_systems") if extra else "",
                    extra.get("organ_system") if extra else "",
                    "; ".join(organ_systems),
                )
            )
            if not organ_system_text:
                organ_system_text = _infer_safety_organ_systems(
                    record.get("type_of_study"),
                    extra.get("study_title") if extra else "",
                    extra.get("title") if extra else "",
                    extra.get("endpoints_assays") if extra else "",
                    extra.get("endpoints") if extra else "",
                    findings_text,
                    "; ".join(narrative_fragments),
                )
            glp_text = _first_non_empty(
                (
                    _normalize_glp_value(record.get("glp_compliance")),
                    _normalize_glp_value(extra.get("glp_compliance") if extra else ""),
                    _normalize_glp_value(extra.get("glp_status") if extra else ""),
                    _normalize_glp_value(extra.get("glp") if extra else ""),
                    _infer_glp_compliance(
                        extra.get("qa_statement") if extra else "",
                        extra.get("glp_statement") if extra else "",
                        extra.get("study_title") if extra else "",
                        extra.get("title") if extra else "",
                        findings_text,
                        "; ".join(narrative_fragments),
                    ),
                )
            )
            candidates.append(
                {
                    "organ systems evaluated": organ_system_text,
                    "species strain": _first_non_empty(
                        (
                            record.get("test_system"),
                            f"{record.get('species')}; {record.get('strain')}"
                            if record.get("species") and record.get("strain")
                            else "",
                            record.get("species"),
                        )
                    ),
                    "method of administration": str(
                        record.get("method_of_administration") or ""
                    ),
                    "doses": _first_non_empty(
                        (
                            extra.get("dose_levels") if extra else "",
                            extra.get("dose_level") if extra else "",
                            extra.get("dose") if extra else "",
                            doses_text,
                        )
                    ),
                    "gender and no per group": _first_non_empty(
                        (
                            extra.get("gender_group") if extra else "",
                            extra.get("sex_and_n_per_group") if extra else "",
                            sex_group_text,
                        )
                    ),
                    "noteworthy findings": findings_text,
                    "glp compliance": glp_text,
                    "study number": _format_study_number_display(study_number, extra),
                    "location in ctd": str(record.get("location_in_ctd") or ""),
                }
            )

    for asset in context.get("table_assets", []) or []:
        if not _asset_matches_safety_pharmacology(asset):
            continue
        preview_rows = asset.get("preview_rows") or []
        for row in preview_rows:
            if not isinstance(row, dict):
                continue
            key_map = {_normalize_header_token(str(key)): key for key in row.keys()}
            study_number = _pick_first_value(row, key_map, "study number")
            if not study_number:
                continue
            normalized_study_number = (
                _select_study_id_from_text(study_number, study_id_candidates)
                or study_number.strip()
            )
            row_blob = " ".join(
                f"{str(key)}: {str(value or '').strip()}"
                for key, value in row.items()
                if str(value or "").strip()
            )
            if not _looks_like_safety_pharmacology_text(row_blob):
                continue
            organ_system_text = _pick_first_value(row, key_map, "organ systems evaluated")
            if not organ_system_text:
                organ_system_text = _infer_safety_organ_systems(row_blob)
            glp_text = _first_non_empty(
                (
                    _normalize_glp_value(_pick_first_value(row, key_map, "glp compliance")),
                    _infer_glp_compliance(row_blob),
                )
            )
            doses_key = key_map.get("doses")
            candidates.append(
                {
                    "organ systems evaluated": organ_system_text,
                    "species strain": _pick_first_value(row, key_map, "species strain"),
                    "method of administration": _pick_first_value(
                        row, key_map, "method of administration"
                    ),
                    "doses": (
                        str(row.get(doses_key or "") or "").strip() if doses_key else ""
                    ),
                    "gender and no per group": _pick_first_value(
                        row, key_map, "gender and no per group", "gender", "sex"
                    ),
                    "noteworthy findings": _pick_first_value(
                        row, key_map, "noteworthy findings"
                    ),
                    "glp compliance": glp_text,
                    "study number": normalized_study_number,
                    "location in ctd": _pick_first_value(row, key_map, "location in ctd"),
                }
            )
    return _merge_safety_candidate_records(candidates)


def _repair_safety_pharmacology_table(
    tables: Sequence[Dict[str, Any]],
    context: Dict[str, Any],
) -> None:
    """Repair safety pharmacology table."""
    target = next(
        (table for table in tables if str(table.get("subsection") or "") == "2.6.3.4"),
        None,
    )
    if not target:
        return
    candidates = _extract_safety_pharmacology_candidates(context)
    if not candidates:
        return
    columns = [str(col) for col in (target.get("columns") or []) if str(col).strip()]
    if not columns:
        columns = [
            "Organ Systems Evaluated",
            "Species/Strain",
            "Method of Admin.",
            "Doses (mg/kg)",
            "Gender and No. per Group",
            "Noteworthy Findings",
            "GLP Compliance",
            "Study Number",
            "Location in CTD",
        ]
        target["columns"] = columns
    display_by_key: Dict[str, str] = {}
    for candidate in candidates:
        raw = str(candidate.get("study number") or "").strip()
        if not raw:
            continue
        canonical = _canonical_study_number(raw)
        key = canonical.upper() if canonical else raw.upper()
        if not key:
            continue
        current = display_by_key.get(key, "")
        display_by_key[key] = _merge_text_segments(current, raw) if current else raw

    candidate_rows = _rows_from_token_candidates(columns, candidates)
    existing_rows = [
        row for row in (target.get("rows") or []) if isinstance(row, dict)
    ]
    study_col = next(
        (col for col in columns if _normalize_header_token(col) == "study number"), None
    )
    if not existing_rows:
        if study_col:
            for row in candidate_rows:
                raw = str(row.get(study_col) or "").strip()
                canonical = _canonical_study_number(raw)
                key = canonical.upper() if canonical else raw.upper()
                display = display_by_key.get(key, "")
                if display:
                    row[study_col] = display
        target["rows"] = candidate_rows
        return
    if not study_col:
        target["rows"] = existing_rows
        return

    def row_key(row: Dict[str, Any]) -> str:
        """Row key."""
        raw = str(row.get(study_col) or "").strip()
        canonical = _canonical_study_number(raw)
        return canonical.upper() if canonical else raw.upper()

    candidate_by_key: Dict[str, Dict[str, str]] = {}
    for row in candidate_rows:
        key = row_key(row)
        if key:
            candidate_by_key[key] = row

    merged_rows: List[Dict[str, str]] = []
    consumed: Set[str] = set()
    for row in existing_rows:
        normalized_row = {col: str(row.get(col) or "").strip() for col in columns}
        key = row_key(normalized_row)
        supplement = candidate_by_key.get(key) if key else None
        if supplement and key:
            consumed.add(key)
            for col in columns:
                current = str(normalized_row.get(col) or "").strip()
                value = str(supplement.get(col) or "").strip()
                if not value:
                    continue
                if not current:
                    normalized_row[col] = value
                    continue
                token = _normalize_header_token(col)
                if (
                    token in {"study number", "organ systems evaluated"}
                    and current.lower() != value.lower()
                ):
                    normalized_row[col] = _merge_text_segments(current, value)
            display = display_by_key.get(key, "")
            if display:
                normalized_row[study_col] = display
        merged_rows.append(normalized_row)

    for key, row in candidate_by_key.items():
        if key in consumed:
            continue
        candidate_row = {col: str(row.get(col) or "").strip() for col in columns}
        display = display_by_key.get(key, "")
        if display:
            candidate_row[study_col] = display
        merged_rows.append(candidate_row)
    target["rows"] = merged_rows


def _first_non_empty(values: Sequence[Any]) -> str:
    """First non empty."""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _display_title_from_path(value: str) -> str:
    """Display title from path."""
    if not value:
        return ""
    name = Path(str(value)).name
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name.strip()


def _extract_overview_candidates_from_ncd(
    context: Dict[str, Any]
) -> List[Dict[str, str]]:
    """Extract overview candidates from ncd."""
    candidates: List[Dict[str, str]] = []
    for record in _extract_ncd_study_records(context):
        candidates.append(
            {
                "type_of_study": str(record.get("type_of_study") or ""),
                "test_system": str(record.get("test_system") or ""),
                "method_of_administration": str(
                    record.get("method_of_administration") or ""
                ),
                "testing_facility": str(record.get("testing_facility") or ""),
                "study_number": str(record.get("study_number") or ""),
                "location_in_ctd": str(record.get("location_in_ctd") or ""),
            }
        )
    return candidates


def _extract_overview_candidates(context: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract overview candidates."""
    candidates: List[Dict[str, str]] = []
    seen: set[str] = set()
    ncd_ids: set[str] = set()
    for candidate in _extract_overview_candidates_from_ncd(context):
        study_number = str(candidate.get("study_number") or "").strip()
        if not study_number:
            continue
        key = study_number.upper()
        if key in seen:
            continue
        candidates.append(candidate)
        seen.add(key)
        ncd_ids.add(key)

    # Fallback study IDs are provided by context when extracted study records are not
    # available; avoid mixing low-fidelity IDs into otherwise complete NCD records.
    if not ncd_ids:
        for study_number in context.get("ncd_study_ids", []) or []:
            value = str(study_number or "").strip()
            if not value:
                continue
            canonical = _canonical_study_number(value)
            normalized = canonical or value
            key = normalized.upper()
            if key in seen:
                continue
            candidates.append(
                {
                    "type_of_study": "",
                    "test_system": "",
                    "method_of_administration": "",
                    "testing_facility": "",
                    "location_in_ctd": "",
                    "study_number": normalized,
                }
            )
            seen.add(key)
            ncd_ids.add(key)

    logger.debug(
        "overview: table_assets=%s preview_rows_total=%s section_sources=%s",
        len(context.get("table_assets") or []),
        sum(len(a.get("preview_rows") or []) for a in context.get("table_assets") or []),
        len(context.get("section_sources") or []),
    )
    for asset in context.get("table_assets", []) or []:
        preview_rows = asset.get("preview_rows") or []
        for row in preview_rows:
            if not isinstance(row, dict):
                continue
            key_map = {_normalize_header_token(str(key)): key for key in row.keys()}
            study_key = key_map.get("study number")
            if not study_key:
                for fallback in ("study id", "study no", "study #", "report number"):
                    study_key = key_map.get(fallback)
                    if study_key:
                        break
            type_key = key_map.get("type of study")
            if not type_key:
                for fallback in ("study title", "study description", "title"):
                    type_key = key_map.get(fallback)
                    if type_key:
                        break
            if not study_key:
                continue
            study_number = str(row.get(study_key) or "").strip()
            if not study_number:
                continue
            study_ids = _extract_study_ids(study_number)
            if not study_ids:
                continue
            study_id = study_ids[0]
            if ncd_ids and study_id.upper() not in ncd_ids:
                continue
            if study_id.upper() in seen:
                continue
            candidates.append(
                {
                    "type_of_study": (
                        str(row.get(type_key) or "").strip() if type_key else ""
                    ),
                    "test_system": _pick_first_value(
                        row, key_map, "test system", "species strain"
                    ),
                    "method_of_administration": str(
                        row.get(key_map.get("method of administration") or "") or ""
                    ).strip(),
                    "testing_facility": str(
                        row.get(key_map.get("testing facility") or "") or ""
                    ).strip(),
                    "location_in_ctd": str(
                        row.get(key_map.get("location in ctd") or "") or ""
                    ).strip(),
                    "study_number": study_id,
                }
            )
            seen.add(study_id.upper())
    return candidates


def _repair_overview_table(
    tables: Sequence[Dict[str, Any]],
    context: Dict[str, Any],
) -> None:
    """Repair overview table."""
    target = next(
        (table for table in tables if str(table.get("subsection") or "") == "2.6.3.1"),
        None,
    )
    if not target:
        return
    columns = [str(col) for col in (target.get("columns") or [])]
    if not columns:
        return
    study_col = next(
        (col for col in columns if _normalize_header_token(col) == "study number"), None
    )
    if not study_col:
        return
    location_col = next(
        (
            col
            for col in columns
            if _normalize_header_token(col) == "location in ctd"
        ),
        None,
    )

    candidates = _extract_overview_candidates(context)
    if not candidates:
        return
    allowed_ids: set[str] = set()
    for candidate in candidates:
        for study_id in _extract_study_ids(str(candidate.get("study_number") or "")):
            allowed_ids.add(study_id.upper())

    def candidate_text(candidate: Dict[str, str]) -> str:
        """Candidate text."""
        parts = [
            candidate.get("type_of_study", ""),
            candidate.get("test_system", ""),
            candidate.get("method_of_administration", ""),
            candidate.get("testing_facility", ""),
            candidate.get("location_in_ctd", ""),
        ]
        return " ".join(part for part in parts if part)

    candidate_tokens = [(_tokenize_text(candidate_text(c)), c) for c in candidates]
    candidate_index_by_study = {
        candidate.get("study_number", ""): idx
        for idx, candidate in enumerate(candidates)
    }

    matched_indices: set[int] = set()
    rows = target.get("rows")
    if not isinstance(rows, list):
        rows = []
        target["rows"] = rows
    for row in rows:
        if not isinstance(row, dict):
            continue
        current = str(row.get(study_col) or "").strip()
        normalized_current = _extract_study_ids(current)
        if current and not normalized_current:
            row[study_col] = ""
            current = ""
        if normalized_current:
            canonical_id = normalized_current[0]
            if allowed_ids and canonical_id.upper() not in allowed_ids:
                row[study_col] = ""
                continue
            row[study_col] = canonical_id
            if canonical_id in candidate_index_by_study:
                matched_indices.add(candidate_index_by_study[canonical_id])
            continue
        row_text = " ".join(
            str(row.get(col) or "")
            for col in columns
            if _normalize_header_token(col) not in {"study number", "location in ctd"}
        ).strip()
        row_tokens = _tokenize_text(row_text)
        if not row_tokens:
            continue
        best_score = 0.0
        best_index: Optional[int] = None
        for idx, (tokens, candidate) in enumerate(candidate_tokens):
            if not tokens:
                continue
            overlap = row_tokens & tokens
            score = len(overlap) / max(len(tokens), 1)
            if score > best_score:
                best_score = score
                best_index = idx
        if best_index is not None and best_score >= 0.25:
            row[study_col] = candidates[best_index]["study_number"]
            matched_indices.add(best_index)

    # Append any unmatched extracted rows.
    for idx, candidate in enumerate(candidates):
        if idx in matched_indices:
            continue
        new_row = {col: "" for col in columns}
        for col in columns:
            key = _normalize_header_token(col)
            if key == "type of study":
                new_row[col] = candidate.get("type_of_study", "")
            elif key == "test system":
                new_row[col] = candidate.get("test_system", "")
            elif key == "method of administration":
                new_row[col] = candidate.get("method_of_administration", "")
            elif key == "testing facility":
                new_row[col] = candidate.get("testing_facility", "")
            elif key == "location in ctd":
                new_row[col] = candidate.get("location_in_ctd", "")
            elif key == "study number":
                new_row[col] = candidate.get("study_number", "")
        rows.append(new_row)

    candidate_by_id = {
        str(candidate.get("study_number") or "").strip().upper(): candidate
        for candidate in candidates
        if candidate.get("study_number")
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        current_ids = _extract_study_ids(str(row.get(study_col) or ""))
        if not current_ids:
            continue
        canonical_id = current_ids[0]
        if allowed_ids and canonical_id.upper() not in allowed_ids:
            continue
        row[study_col] = canonical_id
        candidate = candidate_by_id.get(canonical_id.upper())
        if not candidate:
            continue
        for col in columns:
            if str(row.get(col) or "").strip():
                continue
            key = _normalize_header_token(col)
            if key == "type of study":
                row[col] = candidate.get("type_of_study", "")
            elif key == "test system":
                row[col] = candidate.get("test_system", "")
            elif key == "method of administration":
                row[col] = candidate.get("method_of_administration", "")
            elif key == "testing facility":
                row[col] = candidate.get("testing_facility", "")
            elif key == "location in ctd":
                row[col] = candidate.get("location_in_ctd", "")
        if (
            location_col
            and not str(row.get(location_col) or "").strip()
            and candidate.get("location_in_ctd")
        ):
            row[location_col] = candidate.get("location_in_ctd", "")

    deduped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        study_id = str(row.get(study_col) or "").strip()
        parsed_ids = _extract_study_ids(study_id)
        if not parsed_ids:
            continue
        canonical_id = parsed_ids[0]
        if allowed_ids and canonical_id.upper() not in allowed_ids:
            continue
        row[study_col] = canonical_id
        score = sum(1 for col in columns if str(row.get(col) or "").strip())
        current = deduped.get(canonical_id)
        if not current or score > current.get("_score", 0):
            row_copy = dict(row)
            row_copy["_score"] = score
            deduped[canonical_id] = row_copy
    if deduped:
        rows[:] = [
            {k: v for k, v in entry.items() if k != "_score"}
            for entry in deduped.values()
        ]


def _tabulated_template_entries_for_section(section: str) -> List[Dict[str, Any]]:
    """Tabulated template entries for section."""
    entries = _load_ind_template_entries()
    matches: List[Dict[str, Any]] = []
    target = section.strip()
    if not target:
        return matches
    target_lower = target.lower()
    token_match = re.search(r"\d+(?:\.\d+)+", target_lower)
    if token_match:
        target_lower = token_match.group(0)
    if target_lower.endswith(".x") or target_lower.endswith(".*"):
        target_lower = target_lower[:-2]
    target_lower = target_lower.rstrip(".")
    for entry in entries:
        raw = entry.get("raw") or {}
        if not isinstance(raw, dict):
            continue
        if not (raw.get("Table Description Boilerplate") or raw.get("Columns Headers")):
            continue
        sec = str(entry.get("section") or "").strip()
        sub = str(entry.get("subsection") or "").strip()
        if sub and sub.lower() == target_lower:
            matches.append(entry)
            continue
        if sec and sec.lower() == target_lower:
            matches.append(entry)
            continue
        if sec and sec.lower().startswith(target_lower):
            matches.append(entry)
    return matches


def _read_table_json_preview(
    s3_client: Any,
    *,
    bucket: str,
    key: str,
    max_rows: int,
) -> List[Dict[str, Any]]:
    """Read table json preview."""
    raw = _read_s3_text(s3_client, bucket, key)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        if all(isinstance(row, dict) for row in payload):
            return [row for row in payload[:max_rows] if isinstance(row, dict)]
        if all(isinstance(row, list) for row in payload):
            header_idx: Optional[int] = None
            for idx, row in enumerate(payload):
                if not row:
                    continue
                normalized = [_normalize_header_token(str(cell)) for cell in row]
                hits = sum(
                    1
                    for token in normalized
                    if token
                    in {
                        "study number",
                        "type of study",
                        "test system",
                        "organ systems evaluated",
                        "noteworthy findings",
                    }
                )
                if hits >= 2 or "study number" in normalized:
                    header_idx = idx
                    break
            if header_idx is None:
                return []
            headers = [str(cell).strip() for cell in payload[header_idx]]
            rows: List[Dict[str, Any]] = []
            for row in payload[header_idx + 1 :]:
                if not isinstance(row, list):
                    continue
                row_dict = {
                    headers[i]: row[i] if i < len(row) else ""
                    for i in range(len(headers))
                }
                rows.append(row_dict)
                if len(rows) >= max_rows:
                    break
            return rows
    return []


def _build_tabulated_context(
    db: Session,
    *,
    section: str,
    tenant_id: str,
    project_id: str,
    bucket: str,
    max_table_rows: int = 10,
    max_tables: int = 50,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Build tabulated context."""
    if section.startswith("2.6.3"):
        max_table_rows = max(max_table_rows, 50)
    project_name = fetch_project_name(db, project_id)
    project_like = f"%/{project_name}/%" if project_name else "%"

    module4_sections, mapping_entries, targets = module4_sections_for_ctd_targets(
        section
    )
    mapping_filter = "prefix"
    fallback_section: Optional[str] = None
    if section.startswith("2.6."):
        exact_sections = set()
        filtered_mappings: List[Dict[str, Any]] = []
        for entry in mapping_entries:
            matched_targets = [
                target_entry
                for target_entry in entry.get("targets", [])
                if target_entry.get("section") == section
            ]
            if matched_targets:
                filtered = dict(entry)
                filtered["matched_targets"] = matched_targets
                filtered_mappings.append(filtered)
                exact_sections.add(entry.get("module4_section"))
        if filtered_mappings:
            module4_sections = sorted(s for s in exact_sections if s)
            mapping_entries = filtered_mappings
            mapping_filter = "exact"
        elif mapping_entries:
            fallback_section = f"{section}.*"

    sources = fetch_section_sources(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
        module4_sections=module4_sections,
    )
    project_document_keys = fetch_project_document_keys(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
    )
    document_keys = fetch_document_keys_for_sections(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
        module4_sections=module4_sections,
    )
    ncd_study_ids = fetch_study_ids_for_sections(
        db,
        project_id=project_id,
        module4_sections=module4_sections,
    )
    ncd_payload: Dict[str, Any] = {}
    try:
        ncd_payload = fetch_ncd_payload(
            db,
            project_id=project_id,
            module4_sections=module4_sections,
        )
    except Exception as exc:
        ncd_payload = {}
        logger.warning(
            "tabulated ctx: failed to fetch ncd payload for section=%s project=%s: %s",
            section,
            project_id,
            exc,
        )

    available_sections = _sections_with_material_data(
        module4_sections,
        section_sources=sources,
        document_keys=document_keys,
        ncd_payload=ncd_payload,
    )
    if set(available_sections) != set(module4_sections):
        module4_sections = available_sections
        mapping_entries = _filter_mapping_entries_by_module4_sections(
            mapping_entries,
            module4_sections,
        )
        sources = fetch_section_sources(
            db,
            tenant_id=tenant_id,
            bucket=bucket,
            project_like=project_like,
            module4_sections=module4_sections,
        )
        document_keys = fetch_document_keys_for_sections(
            db,
            tenant_id=tenant_id,
            bucket=bucket,
            project_like=project_like,
            module4_sections=module4_sections,
        )
        ncd_study_ids = fetch_study_ids_for_sections(
            db,
            project_id=project_id,
            module4_sections=module4_sections,
        )
        try:
            ncd_payload = fetch_ncd_payload(
                db,
                project_id=project_id,
                module4_sections=module4_sections,
            )
        except Exception as exc:
            ncd_payload = {}
            logger.warning(
                "tabulated ctx: failed to refetch ncd payload for section=%s project=%s: %s",
                section,
                project_id,
                exc,
            )

    assets = fetch_assets_for_sections(
        db,
        tenant_id=tenant_id,
        bucket=bucket,
        project_like=project_like,
        module4_sections=module4_sections,
        asset_type="table",
    )

    s3_client: Any = None
    s3_unavailable = False
    try:
        s3_client = _boto3_client("s3")
    except HTTPException as exc:
        detail_text = str(getattr(exc, "detail", "") or "")
        if exc.status_code == 500 and "boto3 is required for S3 operations" in detail_text:
            s3_unavailable = True
            logger.warning(
                "tabulated ctx: boto3 unavailable; proceeding without S3 table previews/listing "
                "(section=%s project=%s)",
                section,
                project_id,
            )
        else:
            raise
    s3_listing_keys: List[str] = []
    table_assets: List[Dict[str, Any]] = []
    selected_assets: List[Dict[str, Any]] = []
    if max_tables > 0 and assets:
        assets_by_document: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in assets:
            key = str(row.get("s3_key") or "")
            document_key = key.split(".pdf.tables/", 1)[0] if key else ""
            assets_by_document[document_key].append(row)
        document_asset_keys = sorted(assets_by_document.keys())
        while len(selected_assets) < max_tables:
            added_in_round = False
            for document_key in document_asset_keys:
                bucket_rows = assets_by_document.get(document_key) or []
                if not bucket_rows:
                    continue
                selected_assets.append(bucket_rows.pop(0))
                added_in_round = True
                if len(selected_assets) >= max_tables:
                    break
            if not added_in_round:
                break

    for row in selected_assets:
        extra = _normalize_extra_attributes(row.get("extra_attributes"))
        json_key = extra.get("json_key")
        preview_rows: List[Dict[str, Any]] = []
        if json_key and s3_client is not None:
            preview_rows = _read_table_json_preview(
                s3_client,
                bucket=row.get("s3_bucket") or bucket,
                key=json_key,
                max_rows=max_table_rows,
            )
        elif json_key and s3_client is None:
            logger.debug(
                "tabulated ctx: skipping preview_rows for asset %s because S3 client is unavailable",
                row.get("id"),
            )
        else:
            logger.debug(
                "tabulated ctx: asset %s missing json_key (section=%s, module4=%s, page=%s idx=%s)",
                row.get("id"),
                section,
                row.get("section_number") if "section_number" in row else None,
                row.get("page_number"),
                row.get("index_on_page"),
            )
        if not preview_rows:
            logger.debug(
                "tabulated ctx: no preview_rows for asset %s (json_key=%s, columns=%s, row_count=%s)",
                row.get("id"),
                json_key,
                extra.get("columns"),
                extra.get("row_count"),
            )
        table_assets.append(
            {
                "id": row.get("id"),
                "s3_bucket": row.get("s3_bucket"),
                "s3_key": row.get("s3_key"),
                "json_key": json_key,
                "caption": row.get("caption"),
                "description": row.get("description"),
                "keywords": row.get("keywords") or [],
                "page_number": row.get("page_number"),
                "index_on_page": row.get("index_on_page"),
                "extra_attributes": extra,
                "columns": extra.get("columns") or [],
                "row_count": extra.get("row_count"),
                "preview_rows": preview_rows,
            }
        )

    if s3_client is not None:
        s3_listing_keys = _list_module4_study_keys(
            s3_client,
            bucket=bucket,
            project_name=project_name,
            module4_sections=module4_sections,
            seeds=[
                *(asset.get("s3_key") for asset in table_assets if asset.get("s3_key")),
                *(source.get("s3_key") for source in sources if source.get("s3_key")),
                *document_keys,
                *project_document_keys,
            ],
        )

    template_entries = _tabulated_template_entries_for_section(section)
    table_specs: List[Dict[str, Any]] = []
    for entry in template_entries:
        raw = entry.get("raw") or {}
        if not isinstance(raw, dict):
            continue
        columns_header = (
            raw.get("Columns Headers")
            or raw.get("Column Header")
            or raw.get("Column Headers")
        )
        table_specs.append(
            {
                "section": entry.get("section"),
                "subsection": entry.get("subsection"),
                "subsection_header": entry.get("subsection_header"),
                "ind_requirement": raw.get("IND Requirement"),
                "table_description": raw.get("Table Description Boilerplate"),
                "row_content": raw.get("Row Content"),
                "columns_header": columns_header,
                "columns": _parse_columns_header(columns_header),
                "column_examples": raw.get("Column Example Values"),
                "column_mapping": raw.get("Column Value in Module 4 Location Mapping"),
            }
        )

    mapped_target_sections = _mapped_target_sections_for_request(
        section=section,
        mapping_entries=mapping_entries,
    )
    if section.startswith("2.6."):
        table_specs = _filter_section_entries_for_targets(
            table_specs,
            target_sections=mapped_target_sections,
        )
    table_specs = _align_pharmacology_tabulated_specs(section, table_specs)

    preview_row_total = sum(
        len(asset.get("preview_rows") or []) for asset in table_assets
    )
    table_assets_with_preview = sum(
        1 for asset in table_assets if asset.get("preview_rows")
    )
    table_assets_with_json = sum(1 for asset in table_assets if asset.get("json_key"))
    warnings: List[str] = []
    if not project_name:
        warnings.append("Project name not found; project_like fallback '%' used.")
    if not module4_sections:
        warnings.append(
            "No Module 4 sections matched the CTD target; check mapping or use a subsection."
        )
    if not mapping_entries:
        warnings.append("No CTD mapping entries matched the request.")
    if not sources:
        warnings.append("No section sources found for the matched Module 4 sections.")
    if not assets:
        warnings.append("No table assets found for the matched Module 4 sections.")
    if fallback_section:
        warnings.append(
            f"No mapping for {section}; using {fallback_section} module sections."
        )
    if s3_unavailable:
        warnings.append(
            "boto3 unavailable; skipped S3 table preview retrieval and study-key listing."
        )
    ncd_studies_count = len(ncd_payload.get("studies") or [])
    ncd_source_documents_count = len(ncd_payload.get("source_documents") or [])

    debug = {
        "project_name": project_name,
        "project_like": project_like,
        "mapping_filter": mapping_filter,
        "ctd_targets": targets,
        "module4_sections_count": len(module4_sections),
        "mapping_count": len(mapping_entries),
        "section_sources_count": len(sources),
        "table_assets_count": len(assets),
        "table_assets_included": len(table_assets),
        "table_assets_with_json_key": table_assets_with_json,
        "table_assets_with_preview_rows": table_assets_with_preview,
        "table_preview_row_count": preview_row_total,
        "table_specs_count": len(table_specs),
        "document_keys_count": len(document_keys),
        "s3_listing_keys_count": len(s3_listing_keys),
        "ncd_studies_count": ncd_studies_count,
        "ncd_source_documents_count": ncd_source_documents_count,
        "warnings": warnings,
        "table_asset_samples": [
            {
                "id": asset.get("id"),
                "s3_key": asset.get("s3_key"),
                "json_key": asset.get("json_key"),
                "row_count": asset.get("row_count"),
                "preview_rows_count": len(asset.get("preview_rows") or []),
            }
            for asset in table_assets[:3]
        ],
    }

    return {
        "section": section,
        "ctd_targets": targets,
        "module4_sections": module4_sections,
        "mapping": mapping_entries,
        "table_specs": table_specs,
        "table_assets": table_assets,
        "section_sources": _slim_sources([dict(row) for row in sources]),
        "document_keys": document_keys,
        "project_document_keys": project_document_keys,
        "ncd_study_ids": ncd_study_ids,
        "ncd_payload": ncd_payload,
        "s3_listing_keys": s3_listing_keys,
    }, debug


def _merge_tabulated_tables(
    table_specs: Sequence[Dict[str, Any]],
    tables: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge tabulated tables."""
    def _normalize_rows(
        rows: Sequence[Dict[str, Any]] | None, columns: Sequence[str]
    ) -> List[Dict[str, Any]]:
        """Normalize rows."""
        if not rows:
            return []
        if not columns:
            return [row for row in rows if isinstance(row, dict)]
        normalized: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized.append({col: row.get(col, "") for col in columns})
        return normalized

    tables_by_subsection = {
        str(table.get("subsection") or ""): table
        for table in tables
        if isinstance(table, dict)
    }
    merged: List[Dict[str, Any]] = []
    for spec in table_specs:
        subsection = str(spec.get("subsection") or "")
        existing = tables_by_subsection.get(subsection, {})
        spec_columns = spec.get("columns") or []
        existing_rows = existing.get("rows") or []
        columns = spec_columns or existing.get("columns") or []
        rows = _normalize_rows(existing_rows, columns)
        merged.append(
            {
                "subsection": subsection,
                "subsection_header": spec.get("subsection_header"),
                "description": spec.get("table_description"),
                "columns": columns,
                "rows": rows,
                "notes": existing.get("notes") or "",
            }
        )
    _populate_pharmacology_statement_tables(merged)
    return merged


def _normalize_tabulated_columns(value: Any) -> List[str]:
    """Normalize tabulated columns."""
    if not value:
        return []
    if isinstance(value, str):
        parts = re.split(r"[|\n]", value)
        return [_normalize_header_token(part.strip()) for part in parts if part.strip()]
    if isinstance(value, (list, tuple)):
        return [
            _normalize_header_token(str(item)) for item in value if str(item).strip()
        ]
    return []


def _columns_from_rows(rows: Sequence[Dict[str, Any]] | None) -> List[str]:
    """Columns from rows."""
    if not rows:
        return []
    first = rows[0] if isinstance(rows[0], dict) else {}
    return _normalize_tabulated_columns(list(first.keys()))


def _realign_tabulated_tables_by_columns(
    table_specs: Sequence[Dict[str, Any]],
    tables: Sequence[Dict[str, Any]],
) -> None:
    """Realign tabulated tables by columns."""
    spec_columns: Dict[str, List[str]] = {}
    for spec in table_specs:
        subsection = str(spec.get("subsection") or "")
        columns = _normalize_tabulated_columns(spec.get("columns") or [])
        if subsection and columns:
            spec_columns[subsection] = columns

    if not spec_columns:
        return

    for table in tables:
        if not isinstance(table, dict):
            continue
        cols = _normalize_tabulated_columns(table.get("columns") or [])
        if not cols:
            cols = _columns_from_rows(table.get("rows"))
        if not cols:
            continue
        cols_set = set(cols)
        best_subsection: Optional[str] = None
        best_score = 0.0
        for subsection, spec_cols in spec_columns.items():
            spec_set = set(spec_cols)
            if not spec_set:
                continue
            overlap = len(cols_set & spec_set)
            score = overlap / max(len(spec_set), 1)
            if score > best_score:
                best_score = score
                best_subsection = subsection

        current_subsection = str(table.get("subsection") or "")
        current_score = 0.0
        if current_subsection in spec_columns:
            current_set = set(spec_columns[current_subsection])
            current_score = len(cols_set & current_set) / max(len(current_set), 1)

        if (
            best_subsection
            and best_score >= 0.6
            and best_score >= max(current_score + 0.15, 0.75)
        ):
            table["subsection"] = best_subsection


def _build_tabulated_prompt(
    *,
    section: str,
    context: Dict[str, Any],
    user_prompt: Optional[str],
    user_comment: Optional[str],
    previous_tables: Optional[Dict[str, Any]],
    previous_embedding: Any,
) -> tuple[str, str]:
    """Build tabulated prompt."""
    user_prompt = _normalize_user_prompt(user_prompt)
    system_prompt = (
        "You are an expert nonclinical regulatory writer. "
        "Generate CTD Module 2.6 tabulated summaries using only the provided data. "
        "Return JSON only."
    )
    instructions = (
        user_prompt
        if user_prompt
        else (
            "Use the table specs to populate rows from the available table assets and summaries. "
            "If data is missing, leave rows empty and add notes."
        )
    )
    parts = [
        f"CTD Section: {section}",
        f"Instructions: {instructions}",
        "Return JSON with schema: "
        '{"section": "<section>", "tables": [{"subsection": "...", '
        '"subsection_header": "...", "columns": ["..."], "rows": ['
        '{"<col>": "<value>"}], "notes": ""}]}.',
        "Use the table specs' column headers exactly for the columns list and row keys.",
    ]
    if user_comment:
        if previous_tables:
            parts.append(
                f"Previous tables JSON:\n{json.dumps(previous_tables, ensure_ascii=True, indent=2)}"
            )
        embedding_text = _format_embedding_for_prompt(previous_embedding)
        if embedding_text:
            parts.append(f"Previous tables embedding: {embedding_text}")
        parts.append(f"User comment:\n{user_comment}")
        parts.append("Revise the tables to address the comment.")

    context_json = json.dumps(
        _trim_tabulated_context(context), ensure_ascii=True, indent=2, default=str
    )
    parts.append(f"Context data (JSON):\n{context_json}")
    return system_prompt, "\n\n".join(parts)


def _build_topic_title(text: str, max_words: int = 12) -> str:
    """Build topic title."""
    cleaned = (text or "").strip()
    if not cleaned:
        return "Topic"
    words = cleaned.split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]) + " ..."


def _attach_assets_to_topic(
    *,
    assets: Sequence[Dict[str, Any]],
    document_version_id: str,
    page_start: Optional[int],
    page_end: Optional[int],
) -> Dict[str, List[Dict[str, Any]]]:
    """Attach assets to topic."""
    images: List[Dict[str, Any]] = []
    tables: List[Dict[str, Any]] = []
    window_start = page_start
    window_end = page_end
    for asset in assets:
        if str(asset.get("document_version_id")) != str(document_version_id):
            continue
        page_number = asset.get("page_number")
        if window_start is not None and page_number is not None:
            if window_end is None:
                window_end = window_start
            # keep assets on the same page or adjacent pages
            if page_number < window_start - 1 or page_number > window_end + 1:
                continue
        target = tables if asset.get("asset_type") == "table" else images
        target.append(asset)
    return {"images": images, "tables": tables}


def _render_table_html(
    columns: Sequence[str],
    rows: Sequence[Dict[str, Any]],
    caption: Optional[str] = None,
    max_rows: int = 20,
) -> str:
    """Render table html."""
    cols = [str(c) for c in columns] if columns else []
    body_rows = rows[:max_rows] if rows else []
    if not cols and body_rows:
        # derive order from first row
        cols = list(body_rows[0].keys())
    thead = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    tbody_parts: List[str] = []
    for row in body_rows:
        cells = "".join(
            f"<td>{html.escape(str(row.get(col, '')))}</td>" for col in cols
        )
        tbody_parts.append(f"<tr>{cells}</tr>")
    caption_html = f"<caption>{html.escape(caption)}</caption>" if caption else ""
    return f"<table>{caption_html}<thead><tr>{thead}</tr></thead><tbody>{''.join(tbody_parts)}</tbody></table>"
