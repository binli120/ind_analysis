# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

import os
from typing import List

from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

from database.db_interface import NCDRepository
from ncd.extraction.fast_extract import match_fast_extract
from ncd.llm.llm_client import LLMClient
from ncd.database.schemas import ToxStudySummarySchema

TOX_EXTRACTION_SYSTEM_PROMPT = """
You are a senior nonclinical toxicologist. Extract structured data for repeat-dose
toxicology studies for IND CTD Module 2.6.6.

You must return valid JSON strictly following the provided schema.
If data is missing, use null for that field and DO NOT invent values.
"""

TOX_POSITIVE_FINDINGS_PROMPT = """
You are a senior nonclinical toxicologist. Extract only positive findings from
the provided text. Positive findings are observed changes or treatment-related
effects. Ignore negative or "no change" statements.

Return JSON:
{
  "findings": [
    {
      "finding_term": "...",
      "organ_system": "...",
      "organ": "...",
      "severity": "...",
      "adverse": true/false/null,
      "reversible": true/false/null,
      "onset_day": number|null,
      "dose_threshold_mg_per_kg": number|null,
      "excerpt": "<exact quote from input>"
    }
  ]
}
"""

ENABLE_TOX_FAST_EXTRACT = os.getenv("TOX_FAST_EXTRACT", "1").lower() not in {
    "0",
    "false",
    "no",
}


def _coerce_text_field(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
        if not items:
            return None
        return ", ".join(items)
    if isinstance(value, dict):
        fallback = value.get("value") or value.get("text")
        if fallback is not None:
            return str(fallback).strip() or None
        return None
    return str(value).strip() or None


def _normalize_findings(value):
    if not isinstance(value, list):
        return []
    normalized = []
    for item in value:
        if not isinstance(item, dict):
            continue
        finding_term = (
            item.get("finding_term")
            or item.get("finding")
            or item.get("term")
            or item.get("finding_name")
        )
        finding_term = _coerce_text_field(finding_term)
        if not finding_term:
            continue
        cleaned = dict(item)
        cleaned["finding_term"] = finding_term
        normalized.append(cleaned)
    return normalized


def build_tox_extraction_user_prompt(study_text: str, study_id: str) -> str:
    return f"""
Study ID: {study_id}

Text from repeat-dose toxicology report (methods/results/discussion):

\"\"\"{study_text}\"\"\"


Extract:
- NOAEL, LOAEL, limiting organ and finding
- Dose groups (name, sex, n, dose mg/kg and mg/m2 if stated)
- Exposure metrics: AUC, Cmax, t1/2, other PK if present in this block
- Findings: per organ (finding term, severity, adverse yes/no, reversible yes/no, onset day, dose threshold)

Return JSON with keys:
  study_id, species, route, duration_days,
  noael_mg_per_kg, loael_mg_per_kg, limiting_organ, limiting_finding, clinical_multiple,
  dose_groups, exposure_metrics, findings, source_chunk_ids
where dose_groups, exposure_metrics and findings are arrays.
"""


def extract_tox_for_study(
    db: Session, study_id: str, llm: LLMClient
) -> ToxStudySummarySchema | None:
    # Grab relevant chunks associated with this study or its source document
    chunks = (
        db.execute(
            sqltext(
                """
            SELECT tc.id, tc.raw_text, tc.page_from, tc.page_to
            FROM ncd_text_chunk tc
            JOIN ncd_study s ON s.main_source_document_id = tc.source_document_id
            WHERE s.id = :sid
            ORDER BY tc.page_from
        """
            ),
            {"sid": study_id},
        )
        .mappings()
        .all()
    )

    if not chunks:
        return None

    # Concatenate for a first-pass extraction (for large docs you may chunk and merge)
    combined_text = "\n\n".join(c["raw_text"] for c in chunks)
    chunk_ids = [str(c["id"]) for c in chunks]

    user_prompt = build_tox_extraction_user_prompt(combined_text, study_id)

    raw = llm.extract_json(TOX_EXTRACTION_SYSTEM_PROMPT, user_prompt)
    # You might want to wrap with Pydantic to validate
    data = {
        **raw,
        "source_chunk_ids": chunk_ids,
    }
    data["species"] = _coerce_text_field(data.get("species"))
    data["route"] = _coerce_text_field(data.get("route"))
    # Ensure required list fields exist to avoid validation failures
    data.setdefault("dose_groups", [])
    data.setdefault("exposure_metrics", [])
    data["findings"] = _normalize_findings(data.get("findings"))
    # Fill optional scalars when missing
    for key in (
        "duration_days",
        "noael_mg_per_kg",
        "loael_mg_per_kg",
        "limiting_organ",
        "limiting_finding",
        "clinical_multiple",
    ):
        data.setdefault(key, None)
    summary = ToxStudySummarySchema(**data)

    # Persist to DB: ncd_dose_group, ncd_exposure_metric, ncd_finding, ncd_study_safety_summary
    # 1) Safety summary (manual upsert to avoid ON CONFLICT requirement)
    existing_ss = db.execute(
        sqltext(
            """
            SELECT id FROM ncd_study_safety_summary
            WHERE study_id = :sid
            LIMIT 1
        """
        ),
        {"sid": study_id},
    ).scalar()

    if existing_ss:
        db.execute(
            sqltext(
                """
                UPDATE ncd_study_safety_summary
                SET noael_mg_per_kg = :noael,
                    loael_mg_per_kg = :loael,
                    limiting_organ = :organ,
                    limiting_finding = :finding,
                    clinical_multiple = :cm
                WHERE study_id = :sid
            """
            ),
            {
                "sid": study_id,
                "noael": summary.noael_mg_per_kg,
                "loael": summary.loael_mg_per_kg,
                "organ": summary.limiting_organ,
                "finding": summary.limiting_finding,
                "cm": summary.clinical_multiple,
            },
        )
    else:
        db.execute(
            sqltext(
                """
                INSERT INTO ncd_study_safety_summary (
                    study_id, noael_mg_per_kg, loael_mg_per_kg,
                    limiting_organ, limiting_finding, clinical_multiple
                ) VALUES (:sid, :noael, :loael, :organ, :finding, :cm)
            """
            ),
            {
                "sid": study_id,
                "noael": summary.noael_mg_per_kg,
                "loael": summary.loael_mg_per_kg,
                "organ": summary.limiting_organ,
                "finding": summary.limiting_finding,
                "cm": summary.clinical_multiple,
            },
        )

    # 2) Dose groups
    for dg in summary.dose_groups:
        if (
            dg.name is None
            and dg.sex is None
            and dg.n_animals is None
            and dg.dose_mg_per_kg is None
            and dg.dose_mg_per_m2 is None
        ):
            continue
        db.execute(
            sqltext(
                """
                INSERT INTO ncd_dose_group (study_id, name, sex, n_animals, dose_mg_per_kg, dose_mg_per_m2, extra_attributes)
                VALUES (:sid, :name, :sex, :n, :dose_kg, :dose_m2, '{}'::jsonb)
            """
            ),
            {
                "sid": study_id,
                "name": dg.name,
                "sex": dg.sex,
                "n": dg.n_animals,
                "dose_kg": dg.dose_mg_per_kg,
                "dose_m2": dg.dose_mg_per_m2,
            },
        )

    # 3) Findings
    for f in summary.findings:
        if not f.finding_term:
            continue
        db.execute(
            sqltext(
                """
                INSERT INTO ncd_finding (
                    study_id, organ_system, organ, finding_term,
                    severity, adverse, reversible, onset_day,
                    dose_threshold_mg_per_kg, noael_flag
                ) VALUES (
                    :sid, :org_sys, :org, :term, :sev, :adv, :rev, :onset, :dose_thr, :noael
                )
            """
            ),
            {
                "sid": study_id,
                "org_sys": f.organ_system,
                "org": f.organ,
                "term": f.finding_term,
                "sev": f.severity,
                "adv": f.adverse,
                "rev": f.reversible,
                "onset": f.onset_day,
                "dose_thr": f.dose_threshold_mg_per_kg,
                "noael": f.noael_flag,
            },
        )

    # 4) Exposure (if any)
    for em in summary.exposure_metrics:
        if not em.parameter:
            continue
        db.execute(
            sqltext(
                """
                INSERT INTO ncd_exposure_metric (
                    study_id, species, parameter, value, unit, timepoint, clinical_multiple
                ) VALUES (
                    :sid, :species, :param, :val, :unit, :tp, :cm
                )
            """
            ),
            {
                "sid": study_id,
                "species": summary.species,
                "param": em.parameter,
                "val": em.value,
                "unit": em.unit,
                "tp": em.timepoint,
                "cm": em.clinical_multiple,
            },
        )

    _extract_positive_findings(db, study_id, llm, chunks)
    db.commit()
    return summary


def _extract_positive_findings(
    db: Session,
    study_id: str,
    llm: LLMClient,
    chunks: List[dict],
) -> None:
    if not chunks:
        return
    repo = NCDRepository(session=db)
    document_version_id = repo.fetch_document_version_id_for_study(study_id=study_id)

    db.execute(
        sqltext(
            """
            DELETE FROM ncd_finding
            WHERE study_id = :sid AND is_positive IS TRUE
            """
        ),
        {"sid": study_id},
    )

    for chunk in chunks:
        raw_text = chunk.get("raw_text") or ""
        if not raw_text.strip():
            continue
        if ENABLE_TOX_FAST_EXTRACT:
            fast_matches = match_fast_extract(
                raw_text, section_prefixes=("2.6.6", "2.6.7")
            )
            if not fast_matches:
                continue
        prompt = (
            f"Study ID: {study_id}\n"
            f"Chunk ID: {chunk.get('id')}\n"
            f"Pages: {chunk.get('page_from')} - {chunk.get('page_to')}\n\n"
            f'Text:\n"""{raw_text}"""'
        )
        try:
            payload = llm.extract_json(TOX_POSITIVE_FINDINGS_PROMPT, prompt)
        except Exception:
            continue
        findings = payload.get("findings") if isinstance(payload, dict) else None
        if not isinstance(findings, list):
            continue
        asset_ids: List[str] = []
        page_from = chunk.get("page_from")
        page_to = chunk.get("page_to")
        if document_version_id and page_from and page_to:
            asset_ids = repo.fetch_asset_ids_for_pages(
                document_version_id=document_version_id,
                page_start=int(page_from),
                page_end=int(page_to),
            )
        for item in findings:
            if not isinstance(item, dict):
                continue
            excerpt = str(item.get("excerpt") or "").strip()
            finding_term = str(item.get("finding_term") or "").strip()
            if not excerpt or not finding_term:
                continue
            db.execute(
                sqltext(
                    """
                    INSERT INTO ncd_finding (
                        study_id, organ_system, organ, finding_term,
                        severity, adverse, reversible, onset_day,
                        dose_threshold_mg_per_kg, noael_flag,
                        source_chunk_id, context_text, context_page,
                        context_asset_ids, is_positive
                    ) VALUES (
                        :sid, :org_sys, :org, :term,
                        :sev, :adv, :rev, :onset,
                        :dose_thr, :noael,
                        :chunk_id, :context_text, :context_page,
                        CAST(:asset_ids AS uuid[]), :is_positive
                    )
                    """
                ),
                {
                    "sid": study_id,
                    "org_sys": item.get("organ_system"),
                    "org": item.get("organ"),
                    "term": finding_term,
                    "sev": item.get("severity"),
                    "adv": item.get("adverse"),
                    "rev": item.get("reversible"),
                    "onset": item.get("onset_day"),
                    "dose_thr": item.get("dose_threshold_mg_per_kg"),
                    "noael": item.get("noael_flag"),
                    "chunk_id": chunk.get("id"),
                    "context_text": excerpt,
                    "context_page": page_from,
                    "asset_ids": asset_ids,
                    "is_positive": True,
                },
            )
