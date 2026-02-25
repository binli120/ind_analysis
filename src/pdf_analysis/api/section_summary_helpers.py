"""Section-summary and shared CTD helper functions."""

from __future__ import annotations

import ast
import json
import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from ncd.config.ctd_template import load_template_entries, normalize_element_number
from ncd.types.ctd_materials import (
    module4_sections_for_ctd_targets,
    section_number_matches,
)
from pdf_analysis.api.gap_analysis import _gap_key_mentions_module4_section
from pdf_analysis.api.constants import (
    SECTION_PROMPT_MAX_CHARS,
    STUDY_ID_EXT_RE,
    STUDY_ID_PREFIX_RE,
    STUDY_ID_RE,
    STUDY_ID_SKIP_RE,
    STUDY_ID_TRAILERS,
)

logger = logging.getLogger(__name__)

__all__ = [
    "_load_ind_template_entries",
    "_truncate_text",
    "_normalize_summary",
    "_summary_to_text",
    "_trim_section_summary_context",
    "_slim_sources",
    "_slim_key_sections",
    "_template_entries_for_section",
    "_element_entries_for_section",
    "_filter_mapping_entries_by_module4_sections",
    "_sections_with_material_data",
    "_mapped_target_sections_for_request",
    "_filter_section_entries_for_targets",
    "_filter_element_numbers_for_targets",
    "_format_embedding_for_prompt",
]

_IND_TEMPLATE_ENTRIES: List[Dict[str, Any]] | None = None

def _load_ind_template_entries() -> List[Dict[str, Any]]:
    """Load full template entries (section + subsection + headers/content)."""
    global _IND_TEMPLATE_ENTRIES
    if _IND_TEMPLATE_ENTRIES is not None:
        return _IND_TEMPLATE_ENTRIES

    try:
        source_entries = load_template_entries()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Unable to load IND template: %s", exc)
        _IND_TEMPLATE_ENTRIES = []
        return _IND_TEMPLATE_ENTRIES

    entries: List[Dict[str, Any]] = []
    for entry in source_entries:
        if not isinstance(entry, dict):
            continue
        section_id = str(entry.get("Section") or "").strip()
        subsection = str(entry.get("Subsection") or "").strip()
        if not section_id and not subsection:
            continue
        element_raw = str(entry.get("Subsection Element Numbering") or "").strip()
        element_number = normalize_element_number(element_raw)
        entries.append(
            {
                "section": section_id,
                "subsection": subsection or None,
                "element_number": element_number or None,
                "section_header": entry.get("Section Header"),
                "subsection_header": entry.get("Subsection Header"),
                "content": entry.get("Content"),
                "raw": entry,
            }
        )

    _IND_TEMPLATE_ENTRIES = entries
    return _IND_TEMPLATE_ENTRIES


def _truncate_text(text: Optional[str], max_chars: int = 4000) -> str:
    """Truncate text."""
    if not text:
        return ""
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + " ...[truncated]"


def _normalize_summary(value: Any) -> Any:
    """Normalize summary."""
    if value is None:
        return None
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return ""
        try:
            return json.loads(trimmed)
        except json.JSONDecodeError:
            return trimmed
    return value


def _summary_to_text(value: Any) -> str:
    """Summary to text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=True)


def _trim_section_summary_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """Trim section summary context."""
    section_value = str(context.get("section") or "")

    def _row_key(row: Dict[str, Any]) -> tuple[Any, Any, Any]:
        """Row key."""
        return (
            row.get("document_section_id") or row.get("section_id"),
            row.get("section_number"),
            row.get("document_version_id"),
        )

    def _select_rows(
        rows: Sequence[Dict[str, Any]],
        *,
        limit: int,
        head_count: int,
        length_field: str,
    ) -> List[Dict[str, Any]]:
        """Select rows."""
        if limit <= 0:
            return []
        valid_rows = [row for row in rows if isinstance(row, dict)]
        if not valid_rows:
            return []

        selected: List[Dict[str, Any]] = []
        seen: Set[tuple[Any, Any, Any]] = set()
        keep_head = min(head_count, limit, len(valid_rows))

        for row in valid_rows[:keep_head]:
            key = _row_key(row)
            if key in seen:
                continue
            seen.add(key)
            selected.append(row)

        remaining = sorted(
            valid_rows[keep_head:],
            key=lambda row: len(str(row.get(length_field) or "")),
            reverse=True,
        )
        for row in remaining:
            if len(selected) >= limit:
                break
            key = _row_key(row)
            if key in seen:
                continue
            seen.add(key)
            selected.append(row)
        return selected

    def _extract_numeric_evidence(
        text: str, *, max_items: int = 3, max_chars: int = 180
    ) -> List[str]:
        """Extract numeric evidence."""
        if not text:
            return []
        scored: List[tuple[int, str]] = []
        seen: set[str] = set()
        tokens = (
            "mg/kg",
            "q2d",
            "q3d",
            "%",
            "p<",
            "p =",
            "n=",
            "fold",
            "cmax",
            "auc",
            "half-life",
            "half life",
            "terminal",
            "clearance",
            "cl",
            "vss",
            "bioavailability",
            "urine",
            "feces",
            "iv",
            "ip",
            "ic50",
            "ec50",
            "kd",
            "qt",
            "qtc",
            "heart rate",
            "blood pressure",
        )
        for line in re.split(r"[\r\n]+", text):
            for clause in re.split(r"(?<=[.!?;])\s+", line):
                snippet = clause.strip()
                if len(snippet) < 12:
                    continue
                lowered = snippet.lower()
                if not any(ch.isdigit() for ch in snippet):
                    continue
                if not any(token in lowered for token in tokens):
                    continue
                normalized = re.sub(r"\s+", " ", lowered)
                if normalized in seen:
                    continue
                seen.add(normalized)
                score = 0
                if "p<" in lowered or "p =" in lowered:
                    score += 4
                if "%" in lowered:
                    score += 3
                if "mg/kg" in lowered:
                    score += 2
                if "q2d" in lowered or "q3d" in lowered:
                    score += 1
                if "cmax" in lowered or "auc" in lowered:
                    score += 2
                scored.append((score, _truncate_text(snippet, max_chars)))
        scored.sort(key=lambda item: (-item[0], -len(item[1])))
        return [snippet for _, snippet in scored[:max_items]]

    def _coerce_summary_body(raw_value: Any, _depth: int = 0) -> str:
        """Coerce summary body."""
        if _depth > 3:
            return str(raw_value or "").strip()
        if raw_value is None:
            return ""
        if isinstance(raw_value, dict):
            lead = raw_value.get("summary")
            if isinstance(lead, str) and lead.strip():
                return _coerce_summary_body(lead.strip(), _depth + 1)
            return json.dumps(raw_value, ensure_ascii=True)

        text = str(raw_value).strip()
        if not text:
            return ""
        parsed: Any = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            if text.startswith("{") and "summary" in text and "'" in text:
                try:
                    parsed = ast.literal_eval(text)
                except (ValueError, SyntaxError):
                    parsed = None
        if isinstance(parsed, str):
            return _coerce_summary_body(parsed.strip(), _depth + 1)
        if isinstance(parsed, dict):
            lead = parsed.get("summary")
            if isinstance(lead, str) and lead.strip():
                return _coerce_summary_body(lead.strip(), _depth + 1)
            topics = parsed.get("topics")
            if isinstance(topics, list):
                snippets: List[str] = []
                for item in topics[:2]:
                    if not isinstance(item, dict):
                        continue
                    topic_summary = item.get("summary")
                    if isinstance(topic_summary, str) and topic_summary.strip():
                        snippets.append(topic_summary.strip())
                if snippets:
                    return " ".join(snippets)
        return text

    def _trim_sources(
        sources: Sequence[Dict[str, Any]],
        *,
        limit: int,
        max_summary_chars: int,
    ) -> List[Dict[str, Any]]:
        """Trim sources."""
        trimmed: List[Dict[str, Any]] = []
        selected_rows = _select_rows(
            sources, limit=limit, head_count=5, length_field="summary_text"
        )
        section_is_262 = section_value.startswith("2.6.2")
        section_is_264 = section_value.startswith("2.6.4")
        required_groups: tuple[tuple[str, ...], ...] = ()
        protected_patterns: set[str] = set()
        # For 2.6.2, preserve key anti-tumor response studies even when the
        # context needs aggressive trimming.
        if section_is_262:
            required_groups = (
                ("response_of_sc_ht29",),
                ("response_of_sc_colo205",),
                ("response_of_sc_du145",),
                (
                    "antitumor_activity_in_the_mcf-7_human_breast_tumor_orthotopic_xenograft_model",
                    "did_not_reduce_the_growth_of_established_murine_lewis_lung_tumors",
                ),
                (
                    "cross-reactivity_of_lt1009_towards_lipids",
                    "kinetics_and_stoichiometry_of_the_interaction_between_lt1009_and_s1p",
                    "the_binding_kinetics_of_lt1009_to_s1p_as_measured_by_biacore",
                    "molecular_modeling_and_mutagenesis_studies_of_the_humanized_monoclonal_antibody_lt1009_binding_to_the_biolipid_sphingosine-1-phosphate",
                ),
                (
                    "lt1002_inhibits_sphingosine_1-phosphate-induced_migration_of_cultured_human_umbilical_vein_endothelial_cells",
                    "pilot_bioassay_of_manufactured_lt1009_and_lt1002_based_on_neutralization_s1p-induced_il-8_release_from_human_ovarian_skov3_tumor_cells",
                ),
                (
                    "cardiovascular_and_respiratory_safety_pharmacology_study_of_lt1009_administered_by_a_30-minute",
                    "telemetered_cynomolgus_monkeys",
                ),
            )
            protected_patterns = {
                pattern for group in required_groups for pattern in group
            }
            source_rows = [row for row in sources if isinstance(row, dict)]
            required_rows: List[Dict[str, Any]] = []
            for pattern_group in required_groups:
                candidate = next(
                    (
                        row
                        for row in source_rows
                        if any(
                            pattern in str(row.get("section_number") or "").lower()
                            for pattern in pattern_group
                        )
                    ),
                    None,
                )
                if not candidate:
                    continue
                if _row_key(candidate) in {_row_key(row) for row in required_rows}:
                    continue
                required_rows.append(candidate)

            # Compose with guaranteed coverage: keep a small number of head rows,
            # then append all required buckets, then fill remaining from ranked rows.
            effective_limit = max(limit, len(required_rows))
            selected_rows = _select_rows(
                sources, limit=effective_limit, head_count=5, length_field="summary_text"
            )
            composed: List[Dict[str, Any]] = []
            composed_keys: set[tuple[Any, Any, Any]] = set()
            required_keys = {_row_key(row) for row in required_rows}
            head_allowance = max(0, effective_limit - len(required_rows))
            for row in selected_rows:
                if len(composed) >= head_allowance:
                    break
                key = _row_key(row)
                if key in required_keys or key in composed_keys:
                    continue
                composed.append(row)
                composed_keys.add(key)
            for row in required_rows:
                if len(composed) >= effective_limit:
                    break
                key = _row_key(row)
                if key in composed_keys:
                    continue
                composed.append(row)
                composed_keys.add(key)
            for row in selected_rows:
                if len(composed) >= effective_limit:
                    break
                key = _row_key(row)
                if key in composed_keys:
                    continue
                composed.append(row)
                composed_keys.add(key)
            selected_rows = composed

        for row in selected_rows:
            row_section = str(row.get("section_number") or "").lower()
            is_protected = section_is_262 and any(
                pattern in row_section for pattern in protected_patterns
            )
            summary_limit = max_summary_chars
            if is_protected:
                summary_limit = max(summary_limit, 320)
            raw_summary = _coerce_summary_body(row.get("summary_text"))
            numeric_evidence = _extract_numeric_evidence(
                raw_summary,
                max_items=6 if section_is_264 else 4,
                max_chars=220 if section_is_264 else 170,
            )
            if numeric_evidence:
                summary_limit = max(summary_limit, 320)
            trimmed.append(
                {
                    "section_number": row.get("section_number"),
                    "section_title": row.get("section_title"),
                    "summary_text": _truncate_text(
                        raw_summary, summary_limit
                    ),
                    "numeric_evidence": numeric_evidence,
                    "keywords": (row.get("keywords") or [])[:12],
                    "summary_type": row.get("summary_type"),
                    "summary_purpose": row.get("summary_purpose"),
                    "document_section_id": row.get("document_section_id")
                    or row.get("section_id"),
                    "document_version_id": row.get("document_version_id"),
                }
            )
        return trimmed

    def _trim_key_sections(
        sections: Sequence[Dict[str, Any]],
        *,
        limit: int,
        max_text_chars: int,
    ) -> List[Dict[str, Any]]:
        """Trim key sections."""
        trimmed: List[Dict[str, Any]] = []
        for row in _select_rows(sections, limit=limit, head_count=3, length_field="text"):
            trimmed.append(
                {
                    "section_type": row.get("section_type"),
                    "text": _truncate_text(str(row.get("text") or ""), max_text_chars),
                    "page_start": row.get("page_start"),
                    "page_end": row.get("page_end"),
                    "document_version_id": row.get("document_version_id"),
                }
            )
        return trimmed

    def _trim_table_specs(
        entries: Sequence[Dict[str, Any]],
        *,
        limit: int,
        max_columns: int,
    ) -> List[Dict[str, Any]]:
        """Trim table specs."""
        trimmed: List[Dict[str, Any]] = []
        if limit <= 0:
            return trimmed
        for row in entries:
            if len(trimmed) >= limit:
                break
            if not isinstance(row, dict):
                continue
            trimmed.append(
                {
                    "subsection": row.get("subsection"),
                    "subsection_header": row.get("subsection_header"),
                    "columns": (row.get("columns") or [])[:max_columns],
                }
            )
        return trimmed

    def _trim_table_numeric_evidence(
        assets: Sequence[Dict[str, Any]],
        *,
        asset_limit: int,
        rows_per_asset: int,
        max_items: int,
        max_row_chars: int,
    ) -> List[Dict[str, Any]]:
        """Trim table numeric evidence."""
        if max_items <= 0:
            return []

        evidence: List[Dict[str, Any]] = []
        seen: set[str] = set()
        tokens = (
            "mg/kg",
            "%",
            "cmax",
            "auc",
            "half-life",
            "half life",
            "terminal",
            "vss",
            "cl",
            "clearance",
            "bioavailability",
            "urine",
            "feces",
            "iv",
            "ip",
            "peak concentration",
            "target organ",
        )
        for asset in assets[:asset_limit]:
            if not isinstance(asset, dict):
                continue
            source = (
                asset.get("caption")
                or asset.get("description")
                or asset.get("s3_key")
                or asset.get("json_key")
                or f"table_asset:{asset.get('id')}"
            )
            rows = asset.get("preview_rows") or []
            for row in rows[:rows_per_asset]:
                if not isinstance(row, dict):
                    continue
                parts: List[str] = []
                for key, value in row.items():
                    key_text = str(key).strip()
                    value_text = str(value).strip() if value is not None else ""
                    if not key_text or not value_text:
                        continue
                    parts.append(f"{key_text}: {value_text}")
                if not parts:
                    continue
                row_text = "; ".join(parts)
                lowered = row_text.lower()
                if not any(ch.isdigit() for ch in row_text):
                    continue
                if not any(token in lowered for token in tokens):
                    continue
                normalized = re.sub(r"\s+", " ", lowered)
                if normalized in seen:
                    continue
                seen.add(normalized)
                evidence.append(
                    {
                        "source": _truncate_text(str(source), 140),
                        "row": _truncate_text(row_text, max_row_chars),
                    }
                )
                if len(evidence) >= max_items:
                    return evidence
        return evidence

    def _trim_document_detail_profiles(
        profiles: Sequence[Dict[str, Any]],
        *,
        limit: int,
        max_metrics: int,
        max_findings: int,
    ) -> List[Dict[str, Any]]:
        """Trim document detail profiles."""
        trimmed: List[Dict[str, Any]] = []
        if limit <= 0:
            return trimmed
        for row in profiles[:limit]:
            if not isinstance(row, dict):
                continue
            trimmed.append(
                {
                    "study_number": row.get("study_number"),
                    "module4_section": row.get("module4_section"),
                    "type_of_study": _truncate_text(str(row.get("type_of_study") or ""), 120),
                    "test_system": _truncate_text(str(row.get("test_system") or ""), 100),
                    "method_of_administration": _truncate_text(
                        str(row.get("method_of_administration") or ""), 100
                    ),
                    "glp_compliance": row.get("glp_compliance"),
                    "dose_summary_mg_per_kg": _truncate_text(
                        str(row.get("dose_summary_mg_per_kg") or ""), 120
                    ),
                    "group_size_summary": _truncate_text(
                        str(row.get("group_size_summary") or ""), 100
                    ),
                    "exposure_metrics": [
                        _truncate_text(str(item), 90)
                        for item in (row.get("exposure_metrics") or [])[:max_metrics]
                    ],
                    "finding_highlights": [
                        _truncate_text(str(item), 120)
                        for item in (row.get("finding_highlights") or [])[:max_findings]
                    ],
                    "noael_mg_per_kg": row.get("noael_mg_per_kg"),
                    "loael_mg_per_kg": row.get("loael_mg_per_kg"),
                    "limiting_finding": _truncate_text(
                        str(row.get("limiting_finding") or ""), 140
                    ),
                    "traceability": row.get("traceability") or {},
                    "missing_detail_fields": row.get("missing_detail_fields") or [],
                }
            )
        return trimmed

    def _trim_ncd_payload_counts(payload: Dict[str, Any]) -> Dict[str, int]:
        """Trim ncd payload counts."""
        if not isinstance(payload, dict):
            return {}
        return {
            "studies": len(payload.get("studies") or []),
            "dose_groups": len(payload.get("dose_groups") or []),
            "exposure_metrics": len(payload.get("exposure_metrics") or []),
            "findings": len(payload.get("findings") or []),
            "safety_summaries": len(payload.get("safety_summaries") or []),
            "source_documents": len(payload.get("source_documents") or []),
        }

    def _trim_mapping(entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Trim mapping."""
        trimmed: List[Dict[str, Any]] = []
        for row in entries:
            if not isinstance(row, dict):
                continue
            targets = [
                target.get("section")
                for target in (row.get("targets") or [])
                if isinstance(target, dict) and target.get("section")
            ]
            matched = [
                target.get("section")
                for target in (row.get("matched_targets") or [])
                if isinstance(target, dict) and target.get("section")
            ]
            trimmed.append(
                {
                    "module4_section": row.get("module4_section"),
                    "category": row.get("category"),
                    "priority": row.get("priority"),
                    "target_sections": sorted(set(targets)),
                    "matched_target_sections": sorted(set(matched)),
                }
            )
        return trimmed

    def _trim_template_entries(
        entries: Sequence[Dict[str, Any]],
        *,
        limit: int,
        max_content_chars: int,
    ) -> List[Dict[str, Any]]:
        """Trim template entries."""
        trimmed: List[Dict[str, Any]] = []
        for row in entries:
            if len(trimmed) >= limit:
                break
            if not isinstance(row, dict):
                continue
            trimmed.append(
                {
                    "section": row.get("section"),
                    "subsection": row.get("subsection"),
                    "element_number": row.get("element_number"),
                    "section_header": row.get("section_header"),
                    "subsection_header": row.get("subsection_header"),
                    "content": _truncate_text(
                        str(row.get("content") or ""), max_content_chars
                    ),
                }
            )
        return trimmed

    def _trim_elements(
        elements: Sequence[Dict[str, Any]],
        *,
        limit: int,
        max_summary_chars: int,
        max_text_chars: int,
    ) -> List[Dict[str, Any]]:
        """Trim elements."""
        trimmed: List[Dict[str, Any]] = []
        for row in elements[:limit]:
            if not isinstance(row, dict):
                continue
            template_payload = row.get("template") or {}
            slim_template: Dict[str, Any] = {}
            if isinstance(template_payload, dict):
                slim_template = {
                    "section": template_payload.get("section"),
                    "subsection": template_payload.get("subsection"),
                    "element_number": template_payload.get("element_number"),
                    "content": _truncate_text(
                        str(template_payload.get("content") or ""), 280
                    ),
                }
            trimmed.append(
                {
                    "element_number": row.get("element_number"),
                    "section_number": row.get("section_number"),
                    "module4_sections": row.get("module4_sections") or [],
                    "template": slim_template,
                    "sources": _trim_sources(
                        row.get("sources") or [],
                        limit=3,
                        max_summary_chars=max_summary_chars,
                    ),
                    "key_sections": _trim_key_sections(
                        row.get("key_sections") or [],
                        limit=3,
                        max_text_chars=max_text_chars,
                    ),
                }
            )
        return trimmed

    def _build_trimmed(
        *,
        source_limit: int,
        key_limit: int,
        element_limit: int,
        summary_chars: int,
        key_chars: int,
    ) -> Dict[str, Any]:
        """Build trimmed."""
        section_is_pk = section_value.startswith("2.6.4") or section_value.startswith(
            "2.6.5"
        )
        section_is_tox = section_value.startswith("2.6.6") or section_value.startswith(
            "2.6.7"
        ) or section_value.startswith("2.4.4")
        return {
            "section": context.get("section"),
            "ctd_targets": context.get("ctd_targets") or [],
            "module4_sections": (context.get("module4_sections") or [])[:30],
            "mapping": _trim_mapping(context.get("mapping") or []),
            "template_entries": _trim_template_entries(
                context.get("template_entries") or [],
                limit=20,
                max_content_chars=350,
            ),
            "section_sources": _trim_sources(
                context.get("section_sources") or [],
                limit=source_limit,
                max_summary_chars=summary_chars,
            ),
            "section_key_sections": _trim_key_sections(
                context.get("section_key_sections") or [],
                limit=key_limit,
                max_text_chars=key_chars,
            ),
            "table_specs": _trim_table_specs(
                context.get("table_specs") or [],
                limit=10 if section_is_pk else (6 if section_is_tox else 4),
                max_columns=10,
            ),
            "table_numeric_evidence": _trim_table_numeric_evidence(
                context.get("table_assets") or [],
                asset_limit=8,
                rows_per_asset=6,
                max_items=18 if section_is_pk else (14 if section_is_tox else 8),
                max_row_chars=280,
            ),
            "document_detail_profiles": _trim_document_detail_profiles(
                context.get("document_detail_profiles") or [],
                limit=12 if section_is_pk else (10 if section_is_tox else 8),
                max_metrics=8 if section_is_pk else 6,
                max_findings=6 if section_is_tox else 4,
            ),
            "ncd_payload_counts": _trim_ncd_payload_counts(
                context.get("ncd_payload") or {}
            ),
            "elements": _trim_elements(
                context.get("elements") or [],
                limit=element_limit,
                max_summary_chars=summary_chars,
                max_text_chars=key_chars,
            ),
        }

    for source_limit, key_limit, element_limit, summary_chars, key_chars in (
        (24, 8, 6, 360, 220),
        (18, 6, 4, 320, 180),
        (12, 4, 3, 300, 160),
        (8, 2, 2, 260, 120),
    ):
        candidate = _build_trimmed(
            source_limit=source_limit,
            key_limit=key_limit,
            element_limit=element_limit,
            summary_chars=summary_chars,
            key_chars=key_chars,
        )
        if (
            len(json.dumps(candidate, ensure_ascii=True, default=str))
            <= SECTION_PROMPT_MAX_CHARS
        ):
            return candidate

    return _build_trimmed(
        source_limit=5,
        key_limit=0,
        element_limit=0,
        summary_chars=280,
        key_chars=0,
    )


def _slim_sources(sources: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Slim sources."""
    payload: List[Dict[str, Any]] = []
    for row in sources:
        summary = _normalize_summary(row.get("summary") or row.get("summary_text"))
        payload.append(
            {
                "section_number": row.get("section_number"),
                "section_title": row.get("section_title"),
                "summary": summary,
                "summary_text": _summary_to_text(summary),
                "keywords": row.get("keywords") or [],
                "summary_type": row.get("summary_type"),
                "summary_purpose": row.get("summary_purpose"),
                "document_section_id": row.get("document_section_id")
                or row.get("section_id"),
                "document_version_id": row.get("document_version_id"),
                "document_id": row.get("document_id"),
            }
        )
    return payload


def _slim_key_sections(sections: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Slim key sections."""
    payload: List[Dict[str, Any]] = []
    for row in sections:
        payload.append(
            {
                "section_type": row.get("section_type"),
                "text": _truncate_text(row.get("text")),
                "page_start": row.get("page_start"),
                "page_end": row.get("page_end"),
                "char_start": row.get("char_start"),
                "char_end": row.get("char_end"),
                "asset_ids": row.get("asset_ids") or [],
                "document_version_id": row.get("document_version_id"),
            }
        )
    return payload


def _template_entries_for_section(section: str) -> List[Dict[str, Any]]:
    """Template entries for section."""
    entries = _load_ind_template_entries()
    matches: List[Dict[str, Any]] = []
    target = section.strip()
    if not target:
        return matches
    target_lower = target.lower()
    target_element = normalize_element_number(target)
    for entry in entries:
        sec = (entry.get("section") or "").lower()
        sub = (entry.get("subsection") or "").lower()
        element_number = (entry.get("element_number") or "").lower()
        if element_number and target_element and element_number == target_element:
            matches.append(entry)
        elif sub and sub == target_lower:
            matches.append(entry)
        elif sec and sec == target_lower:
            matches.append(entry)
        elif sec and sec.startswith(target_lower):
            matches.append(entry)
    return matches


def _element_entries_for_section(section: str) -> List[str]:
    """Element entries for section."""
    target = normalize_element_number(section)
    if not target:
        return []

    elements: List[str] = []
    seen: Set[str] = set()
    for entry in _load_ind_template_entries():
        raw = entry.get("element_number")
        normalized = normalize_element_number(raw)
        if not normalized or normalized in seen:
            continue
        if (
            normalized == target
            or normalized.startswith(f"{target}.")
            or normalized.startswith(f"{target}-")
        ):
            elements.append(normalized)
            seen.add(normalized)
    return elements


def _filter_mapping_entries_by_module4_sections(
    entries: Sequence[Dict[str, Any]],
    module4_sections: Sequence[str],
) -> List[Dict[str, Any]]:
    """Filter mapping entries by module4 sections."""
    allowed = {str(section).strip() for section in module4_sections if str(section).strip()}
    if not allowed:
        return []
    filtered: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        module4_section = str(entry.get("module4_section") or "").strip()
        if module4_section in allowed:
            filtered.append(entry)
    return filtered


def _sections_with_material_data(
    module4_sections: Sequence[str],
    *,
    section_sources: Sequence[Dict[str, Any]],
    document_keys: Sequence[str],
    ncd_payload: Dict[str, Any],
) -> List[str]:
    """Sections with material data."""
    studies = ncd_payload.get("studies") if isinstance(ncd_payload, dict) else []
    source_documents = (
        ncd_payload.get("source_documents") if isinstance(ncd_payload, dict) else []
    )
    resolved: List[str] = []
    for module4_section in module4_sections:
        section_token = str(module4_section or "").strip()
        if not section_token:
            continue
        has_section_source = any(
            section_number_matches(str(row.get("section_number") or ""), section_token)
            for row in section_sources
            if isinstance(row, dict)
        )
        has_document = any(
            _gap_key_mentions_module4_section(str(key or ""), section_token)
            for key in document_keys
        )
        has_study = any(
            isinstance(row, dict)
            and section_number_matches(
                str(row.get("module4_section") or ""), section_token
            )
            for row in (studies or [])
        )
        has_source_document = any(
            isinstance(row, dict)
            and (
                _gap_key_mentions_module4_section(
                    str(row.get("file_name") or ""), section_token
                )
                or section_number_matches(
                    str(row.get("ctd_section") or ""), section_token
                )
            )
            for row in (source_documents or [])
        )
        if has_section_source or has_document or has_study or has_source_document:
            resolved.append(section_token)
    return sorted(set(resolved))


def _mapped_target_sections_for_request(
    *,
    section: str,
    mapping_entries: Sequence[Dict[str, Any]],
) -> Set[str]:
    """Mapped target sections for request."""
    target = str(section or "").strip()
    if not target:
        return set()
    matched: Set[str] = set()
    for entry in mapping_entries:
        if not isinstance(entry, dict):
            continue
        target_rows = entry.get("matched_targets") or entry.get("targets") or []
        for row in target_rows:
            if not isinstance(row, dict):
                continue
            target_section = str(row.get("section") or "").strip()
            if not target_section:
                continue
            if target_section == target or target_section.startswith(f"{target}."):
                matched.add(target_section)
    return matched


def _filter_section_entries_for_targets(
    entries: Sequence[Dict[str, Any]],
    *,
    target_sections: Set[str],
) -> List[Dict[str, Any]]:
    """Filter section entries for targets."""
    if not target_sections:
        return []
    filtered: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        subsection = str(entry.get("subsection") or "").strip()
        section = str(entry.get("section") or "").strip()
        if subsection and subsection in target_sections:
            filtered.append(entry)
            continue
        if not subsection and section and section in target_sections:
            filtered.append(entry)
    return filtered


def _filter_element_numbers_for_targets(
    element_numbers: Sequence[str],
    *,
    target_sections: Set[str],
) -> List[str]:
    """Filter element numbers for targets."""
    if not target_sections:
        return []
    filtered: List[str] = []
    for element in element_numbers:
        token = str(element or "").strip()
        if not token:
            continue
        if any(
            token == target
            or token.startswith(f"{target}.")
            or token.startswith(f"{target}-")
            for target in target_sections
        ):
            filtered.append(token)
    return filtered


def _format_embedding_for_prompt(embedding: Any) -> str:
    """Format embedding for prompt."""
    if not embedding:
        return ""
    values: List[float] = []
    if isinstance(embedding, str):
        cleaned = embedding.strip()
        if cleaned.startswith("[") and cleaned.endswith("]"):
            cleaned = cleaned[1:-1]
        if cleaned:
            try:
                values = [float(value) for value in cleaned.split(",") if value.strip()]
            except ValueError:
                values = []
    else:
        try:
            values = [float(value) for value in embedding]
        except (TypeError, ValueError):
            values = []
    if not values:
        return ""
    return "[" + ",".join(f"{value:.6f}" for value in values) + "]"
