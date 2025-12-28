# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

from ncd.llm.llm_client import LLMClient
from ncd.database.schemas import PKStudySummarySchema

PK_EXTRACTION_SYSTEM_PROMPT = """
You are a nonclinical pharmacokinetics expert. Extract structured PK parameters
for CTD Module 2.6.4 and 2.6.5.

Return values such as Cmax, AUC, t1/2, CL, Vd, etc.
"""


def build_pk_prompt(text: str, study_id: str) -> str:
    return f"""
Study ID: {study_id}

Text from PK or TK report:

\"\"\"{text}\"\"\"


Extract:
- species, route
- per dose group: parameter (Cmax, AUC, t1/2, etc.), value, unit, timepoint, clinical multiple if mentioned.

Return JSON with keys:
  study_id, species, route, parameters (array), source_chunk_ids.
"""


def extract_pk_for_study(
    db: Session, study_id: str, llm: LLMClient
) -> PKStudySummarySchema | None:
    chunks = (
        db.execute(
            sqltext("""
            SELECT tc.id, tc.raw_text
            FROM ncd_text_chunk tc
            JOIN ncd_study s ON s.main_source_document_id = tc.source_document_id
            WHERE s.id = :sid
            ORDER BY tc.page_from
        """),
            {"sid": study_id},
        )
        .mappings()
        .all()
    )

    if not chunks:
        return None

    combined = "\n\n".join(c["raw_text"] for c in chunks)
    chunk_ids = [str(c["id"]) for c in chunks]

    user_prompt = build_pk_prompt(combined, study_id)
    raw = llm.extract_json(PK_EXTRACTION_SYSTEM_PROMPT, user_prompt)
    raw.setdefault("parameters", [])
    raw["source_chunk_ids"] = chunk_ids
    summary = PKStudySummarySchema(**raw)

    for em in summary.parameters:
        if not em.parameter:
            continue
        db.execute(
            sqltext("""
                INSERT INTO ncd_exposure_metric (
                    study_id, species, parameter, value, unit, timepoint, clinical_multiple
                ) VALUES (:sid, :species, :param, :val, :unit, :tp, :cm)
            """),
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
    db.commit()
    return summary
