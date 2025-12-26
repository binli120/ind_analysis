# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

import json
import re

from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

from ..llm_client import LLMClient
from .topic_rules import TOPIC_RULES

STUDY_TYPE_RULES = [
    (
        "repeat_dose_tox",
        [r"repeated[- ]dose", r"28[- ]day", r"90[- ]day", r"subchronic", r"chronic"],
    ),
    ("single_dose_tox", [r"single[- ]dose", r"acute toxicity"]),
    (
        "genotox_in_vitro",
        [r"ames test", r"bacterial reverse mutation", r"in vitro micronucleus"],
    ),
    ("genotox_in_vivo", [r"in vivo micronucleus", r"comet assay"]),
    ("carcinogenicity", [r"2[- ]year", r"lifetime", r"carcinogenicity"]),
    (
        "safety_pharmacology",
        [r"safety pharmacology", r"cardiovascular", r"cns", r"respiratory"],
    ),
    ("pk_tk", [r"pharmacokinetic", r"toxicokinetic", r"tk", r"pk "]),
    (
        "reproductive_toxicity",
        [r"fertility", r"embryo[- ]fetal", r"perinatal", r"prenatal"],
    ),
]


def classify_study_type_from_title(title: str) -> str | None:
    t = title.lower()
    for stype, patterns in STUDY_TYPE_RULES:
        if any(re.search(p, t) for p in patterns):
            return stype
    return None


def detect_topics(text: str) -> list[str]:
    t = text.lower()
    topics = []
    for name, kws in TOPIC_RULES.items():
        if any(kw in t for kw in kws):
            topics.append(name)
    return list(set(topics))


def classify_study_for_document(
    db: Session, source_document_id: str, llm: LLMClient | None = None
) -> None:
    """
    Heuristic:
      1) Use first page title + early text
      2) Apply rule-based classifier
      3) If inconclusive and LLM client provided, ask LLM
      4) Insert/update ncd_study row
    """
    # get first page text
    row = (
        db.execute(
            sqltext("""
            SELECT page_number, text
            FROM ncd_document_page
            WHERE source_document_id = :sid
            ORDER BY page_number ASC
            LIMIT 1
        """),
            {"sid": source_document_id},
        )
        .mappings()
        .first()
    )

    if not row:
        return

    first_text = row["text"] or ""
    lines = [l.strip() for l in first_text.splitlines() if l.strip()]
    title = lines[0] if lines else "Unknown study"

    study_type = classify_study_type_from_title(title)

    # Optional LLM refinement
    if not study_type and llm:
        system_prompt = (
            "You are a nonclinical regulatory expert. "
            "Classify this study into one of: "
            "[repeat_dose_tox, single_dose_tox, genotox_in_vitro, genotox_in_vivo, "
            "carcinogenicity, safety_pharmacology, pk_tk, reproductive_toxicity]."
        )
        user_prompt = (
            f"Study title and intro:\n\n{first_text}\n\nReturn only the label."
        )
        # TODO: implement llm.generate_text
        stype = llm.generate_text(system_prompt, user_prompt).strip()
        if stype in {s for s, _ in STUDY_TYPE_RULES} | {"pk_tk"}:
            study_type = stype

    # Insert ncd_study if not exists
    study_row = db.execute(
        sqltext("""
            SELECT id FROM ncd_study
            WHERE main_source_document_id = :sid
        """),
        {"sid": source_document_id},
    ).scalar()

    if study_row:
        db.execute(
            sqltext("""
                UPDATE ncd_study
                SET study_type = COALESCE(:stype, study_type),
                    extra_attributes = extra_attributes || CAST(:extra_json AS jsonb)
                WHERE id = :id
            """),
            {"stype": study_type, "extra_json": json.dumps({"title": title}), "id": study_row},
        )
    else:
        new_id = db.execute(
            sqltext("""
                INSERT INTO ncd_study (project_id, main_source_document_id, study_type, extra_attributes)
                SELECT project_id, :sid, :stype, CAST(:extra_json AS jsonb)
                FROM ncd_source_document
                WHERE id = :sid
                RETURNING id;
            """),
            {"sid": source_document_id, "stype": study_type, "extra_json": json.dumps({"title": title})},
        ).scalar()
        study_row = new_id

    db.commit()
