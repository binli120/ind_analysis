#!/usr/bin/env python3
"""Ad-hoc tool for running zero-shot IND classification from the CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"


def _load_env_files() -> None:
    """Load .env files so that local API keys are available."""
    env_files = [REPO_ROOT / ".env.local", REPO_ROOT / ".env"]
    for env_file in env_files:
        if not env_file.exists():
            continue
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key.startswith("#"):
                continue
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_env_files()

def _ensure_src_on_path() -> None:
    """Guarantee that the src directory is importable at runtime."""
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))


def _build_arg_parser() -> argparse.ArgumentParser:
    """Define CLI arguments for the zero-shot classification helper."""
    parser = argparse.ArgumentParser(
        description="Run zero-shot IND section classification on a PDF using the OpenAI metadata generator.",
    )
    parser.add_argument("pdf", type=Path, help="Path to the PDF document to analyse.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optionally limit the number of pages processed.",
    )
    parser.add_argument(
        "--table-engine",
        choices=["auto", "pdfplumber", "camelot", "tabula"],
        default="auto",
        help="Table extraction engine to use. 'auto' evaluates all available engines.",
    )
    parser.add_argument(
        "--ocr-fallback",
        action="store_true",
        help="Run OCR when initial text extraction is empty.",
    )
    parser.add_argument(
        "--table-rows",
        type=int,
        default=200,
        help="Maximum rows returned per table for downstream metadata.",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="OpenAI model identifier to use for metadata generation.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path to write the JSON result. Prints to stdout when omitted.",
    )
    return parser


def _ensure_client(generator: Any) -> None:
    """Exit early with a helpful message when the OpenAI client is not ready."""
    if getattr(generator, "_client", None) is None:
        sys.stderr.write(
            "OpenAI client is not configured. Ensure OPENAI_API_KEY is set in the environment.\n"
        )
        sys.exit(2)


def _run_analysis(
    pdf_path: Path,
    *,
    max_pages: Optional[int],
    table_engine: Optional[str],
    ocr_fallback: bool,
    table_rows: Optional[int],
) -> Dict[str, Any]:
    """Execute the server pipeline runner to obtain markdown/metrics for a PDF."""
    _ensure_src_on_path()
    from pdf_analysis.api.server import _run_pipeline_with_runner  # type: ignore[attr-defined]

    return _run_pipeline_with_runner(
        pdf_path,
        pdf_path.name,
        max_pages=max_pages,
        table_engine=table_engine,
        ocr_fallback=ocr_fallback,
        table_rows=table_rows,
    )


def main() -> None:
    """CLI entrypoint that produces zero-shot classification metadata."""
    parser = _build_arg_parser()
    args = parser.parse_args()

    pdf_path: Path = args.pdf.expanduser().resolve()
    if not pdf_path.exists():
        parser.error(f"PDF not found: {pdf_path}")

    engine = None if args.table_engine == "auto" else args.table_engine

    analysis = _run_analysis(
        pdf_path,
        max_pages=args.max_pages,
        table_engine=engine,
        ocr_fallback=args.ocr_fallback,
        table_rows=args.table_rows,
    )

    _load_env_files()
    _ensure_src_on_path()
    from pdf_analysis.service.ai_metadata import OpenAIMetadataGenerator

    generator = OpenAIMetadataGenerator(model=args.model)
    _ensure_client(generator)

    markdown = analysis.get("markdown") or ""
    metadata = generator(markdown)
    if not metadata:
        sys.stderr.write(
            "Metadata generation returned no result. Check the OpenAI response logs for details.\n"
        )
        sys.exit(3)

    output: Dict[str, Any] = {
        "document": analysis.get("document") or pdf_path.name,
        "ind_metadata": {
            "ind_document_type": metadata.get("ind_document_type"),
            "ind_section_number": metadata.get("ind_section_number"),
            "ind_section_title": metadata.get("ind_section_title"),
            "ind_classification_confidence": metadata.get("ind_classification_confidence"),
        },
        "labels": metadata.get("labels", []),
        "keywords": metadata.get("keywords", []),
        "language": metadata.get("language"),
        "metrics": analysis.get("metrics", {}),
    }

    payload = json.dumps(output, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(f"Wrote zero-shot result to {args.output}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
