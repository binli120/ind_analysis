from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

from ncd.llm_client import LLMClient
from ncd.schemas import ToxStudySummarySchema

TOX_EXTRACTION_SYSTEM_PROMPT = """
You are a senior nonclinical toxicologist. Extract structured data for repeat-dose
toxicology studies for IND CTD Module 2.6.6.

You must return valid JSON strictly following the provided schema.
If data is missing, use null for that field and DO NOT invent values.
"""


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
    summary = ToxStudySummarySchema(**data)

    # Persist to DB: ncd_dose_group, ncd_exposure_metric, ncd_finding, ncd_study_safety_summary
    # 1) Safety summary
    db.execute(
        sqltext("""
            INSERT INTO ncd_study_safety_summary (
                study_id, noael_mg_per_kg, loael_mg_per_kg,
                limiting_organ, limiting_finding, clinical_multiple
            ) VALUES (:sid, :noael, :loael, :organ, :finding, :cm)
            ON CONFLICT (study_id) DO UPDATE SET
              noael_mg_per_kg = EXCLUDED.noael_mg_per_kg,
              loael_mg_per_kg = EXCLUDED.loael_mg_per_kg,
              limiting_organ = EXCLUDED.limiting_organ,
              limiting_finding = EXCLUDED.limiting_finding,
              clinical_multiple = EXCLUDED.clinical_multiple;
        """),
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
        db.execute(
            sqltext("""
                INSERT INTO ncd_dose_group (study_id, name, sex, n_animals, dose_mg_per_kg, dose_mg_per_m2, extra_attributes)
                VALUES (:sid, :name, :sex, :n, :dose_kg, :dose_m2, '{}'::jsonb)
            """),
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
        db.execute(
            sqltext("""
                INSERT INTO ncd_finding (
                    study_id, organ_system, organ, finding_term,
                    severity, adverse, reversible, onset_day,
                    dose_threshold_mg_per_kg, noael_flag
                ) VALUES (
                    :sid, :org_sys, :org, :term, :sev, :adv, :rev, :onset, :dose_thr, :noael
                )
            """),
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
        db.execute(
            sqltext("""
                INSERT INTO ncd_exposure_metric (
                    study_id, species, parameter, value, unit, timepoint, clinical_multiple
                ) VALUES (
                    :sid, :species, :param, :val, :unit, :tp, :cm
                )
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
