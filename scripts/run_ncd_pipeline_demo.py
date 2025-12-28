#!/usr/bin/env python
# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""
Run the NCD ingest + LLM extraction pipeline against a local PDF and persist to the DB.

This is a demo harness that:
- Creates/updates ncd_source_document
- Stores pages, chunks, and embeddings
- Classifies the study
- Runs tox/PK extractors (NOAEL, dose groups, exposure metrics) using a dummy LLM

Usage:
  poetry run python scripts/run_ncd_pipeline_demo.py 4.2.1.1.pdf --project-id <UUID>

Note: The default LLM client is a stub that returns deterministic values. Swap in a real
LLMClient implementation when available.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict

from ncd.llm.llm_client import LLMClient
from ncd.pipeline.pipeline_runner import run_pdf_ingest_and_extract


class DummyLLM(LLMClient):
    """Minimal stub to satisfy extraction without external calls."""

    def extract_json(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model=None,  # type: ignore[override]
    ) -> Dict[str, Any]:
        # Simple branch: if prompt looks PK, return PK payload; otherwise tox payload.
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
        # Simple label to satisfy classification calls
        return "repeat_dose_tox"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run NCD ingest + extraction demo against a PDF.")
    parser.add_argument("pdf", type=Path, help="Path to the PDF to ingest.")
    parser.add_argument("--project-id", required=True, help="Project UUID for DB records.")
    parser.add_argument("--module", default="Module 4", help="Module label to store on source_document.")
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pdf_path: Path = args.pdf
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    result = run_pdf_ingest_and_extract(
        pdf_path=pdf_path,
        project_id=args.project_id,
        module=args.module,
        llm_client=DummyLLM(),
        chunk_max_chars=args.chunk_max_chars,
        embed=not args.no_embed,
    )

    print("\n✅ Pipeline demo complete.")
    print(f"Source document: {result['source_document_id']}")
    print(f"Study:           {result['study_id']}")
    print(f"Chunks inserted: {len(result['chunks'])}")
    print(f"NOAEL:           {result['tox_summary']['noael_mg_per_kg'] if result['tox_summary'] else 'n/a'}")
    print(f"Cmax entries:    {len(result['pk_summary']['parameters']) if result['pk_summary'] else 0}")
    print(f"PK summary:      {bool(result['pk_summary'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
