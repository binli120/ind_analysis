from typing import Any, Dict, List

from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

from ncd.llm_client import LLMClient

MODULE_2_6_SYSTEM_PROMPT = """
You are an expert nonclinical regulatory writer.
Write CTD Module 2.6 toxicology written summary sections based strictly on provided structured data and excerpts.
Do not invent data. Include study IDs and Module 4 cross-references.
"""


def fetch_structured_data_for_section(
    db: Session, project_id: str, section_code: str
) -> List[Dict[str, Any]]:
    """
    Query ncd_ctd_section_mapping -> ncd_study, ncd_study_safety_summary, ncd_finding, ncd_exposure_metric
    """
    rows = (
        db.execute(
            sqltext("""
            SELECT
              s.id AS study_id,
              s.study_type,
              s.species,
              s.route,
              s.duration_days,
              ss.noael_mg_per_kg,
              ss.loael_mg_per_kg,
              ss.limiting_organ,
              ss.limiting_finding,
              ss.clinical_multiple,
              f.organ,
              f.finding_term,
              f.severity,
              f.adverse,
              f.reversible,
              f.dose_threshold_mg_per_kg
            FROM ncd_ctd_section_mapping m
            JOIN ncd_study s ON m.study_id = s.id
            LEFT JOIN ncd_study_safety_summary ss ON ss.study_id = s.id
            LEFT JOIN ncd_finding f ON f.study_id = s.id
            WHERE m.project_id = :pid
              AND m.section_code = :sec;
        """),
            {"pid": project_id, "sec": section_code},
        )
        .mappings()
        .all()
    )
    # You can post-process into a nested dict; for brevity just return rows
    return [dict(r) for r in rows]


def generate_toxicology_section(
    db: Session, project_id: str, section_code: str, llm: LLMClient
) -> str:
    data = fetch_structured_data_for_section(db, project_id, section_code)

    user_prompt = f"""
Generate CTD section {section_code} toxicology written summary.

Structured data (JSON-like) from dictionary:
{data}

Requirements:
- Summarize by species and study type.
- Explicitly mention NOAELs, target organs, key findings, and safety margins.
- Include study IDs in brackets like [Study: <id>].
- Highlight clinically relevant risks and reversibility.
- Do NOT hypothesize beyond provided data.
"""

    text = llm.generate_text(MODULE_2_6_SYSTEM_PROMPT, user_prompt)
    return text
