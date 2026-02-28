#!/usr/bin/env python
# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""
Run DOCX extraction + NCD ingestion + tox/PK extraction in one pass.

Usage:
  poetry run python scripts/run_docx_full_pipeline_demo.py data/module-4-test1.docx \
    --project-id <UUID> --output-dir tmp/docx_pipeline

Notes:
  - Defaults to a dummy LLM to avoid external calls.
  - Use --real-llm to invoke the configured LLM client.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List
from uuid import uuid4

from sqlalchemy import text as sqltext

# Allow running from repo root without installing the package.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from pdf_analysis.export.persist import save_tables, write_text
from pdf_analysis.ingest.docx_text import extract_docx_pages
from pdf_analysis.pipeline import DocxProcessingPipeline
from pdf_analysis.transform.markdown_writer import (
    build_html_document,
    build_markdown_document,
)
from ncd.classification.study_classifier import classify_study_for_document
from ncd.database.db import SessionLocal
from ncd.extraction.pk_extractor import extract_pk_for_study
from ncd.extraction.tox_extractor import extract_tox_for_study
from ncd.ingestion.chunking import chunk_text_by_pages
from ncd.ingestion.embedding import embed_chunks
from ncd.ingestion.pdf_ingestion import create_source_document, sha256_file
from ncd.llm.llm_client import LLMClient


class DummyLLM(LLMClient):
    """Minimal stub to satisfy extraction without external calls."""

    def extract_json(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model=None,  # type: ignore[override]
    ) -> Dict[str, Any]:
        lower_prompt = f"{system_prompt} {user_prompt}".lower()
        if "pk" in lower_prompt or "cmax" in lower_prompt or "auc" in lower_prompt:
            return {
                "study_id": "demo",
                "species": "rat",
                "route": "oral",
                "parameters": [
                    {
                        "dose_group_name": "High",
                        "parameter": "Cmax",
                        "value": 123.4,
                        "unit": "ng/mL",
                        "timepoint": "Day 1",
                        "clinical_multiple": 4.2,
                    },
                    {
                        "dose_group_name": "High",
                        "parameter": "AUC",
                        "value": 9876.5,
                        "unit": "ng*h/mL",
                        "timepoint": "Day 28",
                        "clinical_multiple": 3.8,
                    },
                ],
                "source_chunk_ids": [],
            }

        return {
            "study_id": "demo",
            "species": "rat",
            "route": "oral",
            "duration_days": 28,
            "noael_mg_per_kg": 50,
            "loael_mg_per_kg": 100,
            "limiting_organ": "liver",
            "limiting_finding": "ALT increase",
            "clinical_multiple": 5,
            "dose_groups": [
                {
                    "name": "Low",
                    "sex": "M/F",
                    "n_animals": 10,
                    "dose_mg_per_kg": 10,
                    "dose_mg_per_m2": None,
                },
                {
                    "name": "High",
                    "sex": "M/F",
                    "n_animals": 10,
                    "dose_mg_per_kg": 50,
                    "dose_mg_per_m2": None,
                },
            ],
            "exposure_metrics": [
                {
                    "dose_group_name": "High",
                    "parameter": "Cmax",
                    "value": 123.4,
                    "unit": "ng/mL",
                    "timepoint": "Day 1",
                    "clinical_multiple": 4.2,
                },
                {
                    "dose_group_name": "High",
                    "parameter": "AUC",
                    "value": 9876.5,
                    "unit": "ng*h/mL",
                    "timepoint": "Day 28",
                    "clinical_multiple": 3.8,
                },
            ],
            "findings": [
                {
                    "organ_system": "Hepatic",
                    "organ": "Liver",
                    "finding_term": "ALT increase",
                    "severity": "mild",
                    "adverse": False,
                    "reversible": True,
                    "onset_day": 7,
                    "dose_threshold_mg_per_kg": 50,
                    "noael_flag": False,
                }
            ],
            "source_chunk_ids": [],
        }

    def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        return "repeat_dose_tox"


def _persist_docx_pages(
    db, source_document_id: str, pages: Iterable[Dict[str, Any]]
) -> None:
    db.execute(
        sqltext(
            """
            DELETE FROM ncd_document_page
            WHERE source_document_id = :sid
            """
        ),
        {"sid": source_document_id},
    )

    fallback_page = 1
    for page in pages:
        page_number = page.get("page_number") or fallback_page
        text = page.get("text") or ""
        db.execute(
            sqltext(
                """
                INSERT INTO ncd_document_page (
                    source_document_id, page_number, text
                ) VALUES (
                    :sid, :pnum, :txt
                )
                """
            ),
            {"sid": source_document_id, "pnum": page_number, "txt": text},
        )
        fallback_page += 1

    db.commit()


def _run_core_extraction(docx_path: Path, output_dir: Path | None) -> Dict[str, Any]:
    pipeline = DocxProcessingPipeline()
    result = pipeline.run(docx_path)

    manifest = []
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        tables_dir = output_dir / "tables"
        tables_dir.mkdir(parents=True, exist_ok=True)
        if result.tables:
            manifest = save_tables(docx_path, result.tables, tables_dir)

        markdown = build_markdown_document(docx_path.name, result.pages, manifest)
        html = build_html_document(docx_path.name, result.pages, manifest)
        write_text(output_dir / f"{docx_path.stem}.extracted.md", markdown)
        write_text(output_dir / f"{docx_path.stem}.extracted.html", html)
        if result.quality_report is not None:
            write_text(
                output_dir / f"{docx_path.stem}.quality.json",
                json.dumps(result.quality_report, indent=2),
            )
        if result.quality_markdown is not None:
            write_text(
                output_dir / f"{docx_path.stem}.quality.md",
                result.quality_markdown,
            )

    return {
        "pages": result.pages,
        "tables": result.tables,
        "markdown": result.markdown,
        "quality": result.quality_report,
    }


def _run_tox_pipeline(
    docx_path: Path,
    pages: List[Dict[str, Any]],
    *,
    project_id: str,
    module: str,
    llm: LLMClient,
    chunk_max_chars: int,
    embed: bool,
    force_new_source: bool,
) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        if force_new_source:
            file_hash = sha256_file(str(docx_path))
            suffix = uuid4().hex[:8]
            file_name = f"{docx_path.stem}-{suffix}{docx_path.suffix}"
            source_document_id = db.execute(
                sqltext(
                    """
                    INSERT INTO ncd_source_document (project_id, file_name, module, sha256)
                    VALUES (:pid, :fname, :module, :sha)
                    RETURNING id
                    """
                ),
                {
                    "pid": project_id,
                    "fname": file_name,
                    "module": module,
                    "sha": file_hash,
                },
            ).scalar()
            if source_document_id is None:
                raise RuntimeError("Failed to create source document record")
            db.commit()
            source_document_id = str(source_document_id)
        else:
            source_document_id = create_source_document(
                db=db,
                project_id=project_id,
                file_path=str(docx_path),
                module=module,
            )
        _persist_docx_pages(db, source_document_id, pages)
        chunk_ids = chunk_text_by_pages(
            db, source_document_id, max_chars=chunk_max_chars
        )
        if embed:
            embed_chunks(db, chunk_ids)
        classify_study_for_document(db, source_document_id, llm=llm)
        study_id = db.execute(
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
        if study_id is None:
            raise RuntimeError("Study classification failed to create ncd_study entry.")

        tox_summary = extract_tox_for_study(db, str(study_id), llm)
        pk_summary = extract_pk_for_study(db, str(study_id), llm)
        return {
            "source_document_id": source_document_id,
            "study_id": str(study_id),
            "chunks": chunk_ids,
            "tox_summary": tox_summary.model_dump() if tox_summary else None,
            "pk_summary": pk_summary.model_dump() if pk_summary else None,
        }
    finally:
        db.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run DOCX extraction + ingestion + tox/PK extraction."
    )
    parser.add_argument("docx", type=Path, help="Path to the DOCX to ingest.")
    parser.add_argument(
        "--project-id", required=True, help="Project UUID for DB records."
    )
    parser.add_argument(
        "--module", default="Module 4", help="Module label for source_document."
    )
    parser.add_argument(
        "--chunk-max-chars",
        type=int,
        default=1500,
        help="Max characters per chunk before splitting.",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="Skip embedding generation (inserts chunks only).",
    )
    parser.add_argument(
        "--force-new-source",
        action="store_true",
        help="Always create a new source document record (avoid FK cleanup conflicts).",
    )
    parser.add_argument(
        "--real-llm",
        action="store_true",
        help="Use real LLM client instead of dummy outputs.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory to write extraction artifacts (markdown/html/quality).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    docx_path: Path = args.docx
    if not docx_path.exists():
        raise FileNotFoundError(f"DOCX not found: {docx_path}")

    core_result = _run_core_extraction(docx_path, args.output_dir)
    pages = core_result["pages"] or extract_docx_pages(docx_path)

    llm: LLMClient = LLMClient() if args.real_llm else DummyLLM()
    tox_result = _run_tox_pipeline(
        docx_path,
        pages,
        project_id=args.project_id,
        module=args.module,
        llm=llm,
        chunk_max_chars=args.chunk_max_chars,
        embed=not args.no_embed,
        force_new_source=args.force_new_source,
    )

    print("\nOK DOCX pipeline complete.")
    print(f"Pages extracted:  {len(pages)}")
    print(f"Tables extracted: {len(core_result['tables'])}")
    print(f"Source document:  {tox_result['source_document_id']}")
    print(f"Study:            {tox_result['study_id']}")
    print(f"Chunks inserted:  {len(tox_result['chunks'])}")
    print(
        "NOAEL:            "
        f"{tox_result['tox_summary']['noael_mg_per_kg'] if tox_result['tox_summary'] else 'n/a'}"
    )
    print(
        "PK parameters:    "
        f"{len(tox_result['pk_summary']['parameters']) if tox_result['pk_summary'] else 0}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
