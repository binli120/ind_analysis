"""
Orchestrator to ingest a Module 4 PDF into the NCD schema and run LLM-based
extraction (NOAEL, dose groups, exposure metrics) into the database.

This stitches together:
- page extraction -> ncd_document_page
- chunking -> ncd_text_chunk (+ embeddings)
- study classification -> ncd_study
- LLM extraction -> ncd_dose_group / ncd_exposure_metric / ncd_finding / ncd_study_safety_summary
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

from ncd.classification.study_classifier import classify_study_for_document
from ncd.config import settings
from ncd.db import SessionLocal
from ncd.ingestion.chunking import chunk_text_by_pages
from ncd.ingestion.embedding import embed_chunks
from ncd.ingestion.pdf_ingestion import create_source_document, extract_pages, sha256_file
from ncd.llm_client import LLMClient
from ncd.extraction.tox_extractor import extract_tox_for_study
from ncd.extraction.pk_extractor import extract_pk_for_study


def _ensure_llm(llm: Optional[LLMClient]) -> LLMClient:
    if llm is None:
        raise RuntimeError("LLM client is required for extraction; pass an instance of LLMClient.")
    return llm


def run_pdf_ingest_and_extract(
    pdf_path: Path,
    *,
    project_id: str,
    module: str = "Module 4",
    llm_client: Optional[LLMClient] = None,
    db_session: Optional[Session] = None,
    chunk_max_chars: int = 1500,
    embed: bool = True,
) -> Dict[str, Any]:
    """
    End-to-end helper: ingest PDF pages -> chunks -> embeddings -> classify study -> run LLM extractors.

    Returns a summary dict with IDs and extraction payloads (tox and pk).
    """
    llm = _ensure_llm(llm_client)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    pdf_hash = sha256_file(str(pdf_path))

    # Use provided session or manage our own
    owns_session = False
    db: Session
    if db_session is not None:
        db = db_session
    else:
        db = SessionLocal()
        owns_session = True

    try:
        source_document_id = create_source_document(
            db=db,
            project_id=project_id,
            file_path=str(pdf_path),
            module=module,
        )

        extract_pages(db, source_document_id, str(pdf_path))
        chunk_ids = chunk_text_by_pages(db, source_document_id, max_chars=chunk_max_chars)
        if embed:
            embed_chunks(db, chunk_ids)

        classify_study_for_document(db, source_document_id, llm=llm)
        study_id = (
            db.execute(
                sqltext(
                    """
                    SELECT id FROM ncd_study
                    WHERE main_source_document_id = :sid
                    ORDER BY created_at DESC NULLS LAST
                    LIMIT 1
                    """
                ),
                {"sid": source_document_id},
            ).scalar()
        )
        if study_id is None:
            raise RuntimeError("Study classification failed to create ncd_study entry.")

        tox_summary = extract_tox_for_study(db, str(study_id), llm)
        pk_summary = extract_pk_for_study(db, str(study_id), llm)

        return {
            "project_id": project_id,
            "source_document_id": str(source_document_id),
            "study_id": str(study_id),
            "hash": pdf_hash,
            "chunks": chunk_ids,
            "tox_summary": tox_summary.model_dump() if tox_summary else None,
            "pk_summary": pk_summary.model_dump() if pk_summary else None,
        }
    finally:
        if owns_session:
            db.close()
