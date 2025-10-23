# @author: Bin Lee
# @email: blee@filynai.com

import argparse
import csv
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Optional, Sequence

from pdf_analysis.export.persist import save_tables, write_text
from pdf_analysis.pipeline import PDFProcessingPipeline, PipelineConfig, PipelineResult
from pdf_analysis.transform.markdown_writer import (
    build_html_document,
    build_markdown_document,
)


def _persist_outputs(
    pdf_path: Path, result: PipelineResult, outdir: Path
) -> Dict[str, object]:
    """
    Persist markdown, HTML, table CSV/JSON artefacts, and quality reports.
    Returns a mapping of artefact labels to filesystem paths for logging.
    """
    outdir.mkdir(parents=True, exist_ok=True)

    manifest = save_tables(pdf_path, result.tables, outdir)
    table_csv_paths = [outdir / entry["csv"] for entry in manifest if entry.get("csv")]
    table_json_paths = [outdir / entry["json"] for entry in manifest if entry.get("json")]

    legend_path: Optional[Path] = None
    if manifest:
        legend_entries = []
        for table_entry, manifest_entry in zip(result.tables, manifest):
            dataframe = table_entry.get("dataframe")
            row_count = int(dataframe.shape[0]) if dataframe is not None else 0
            legend_entries.append(
                {
                    "page_number": manifest_entry["page_number"],
                    "index_on_page": manifest_entry["index_on_page"],
                    "engine": manifest_entry["engine"],
                    "rows": row_count,
                    "csv": manifest_entry["csv"],
                    "json": manifest_entry["json"],
                }
            )
        legend_path = outdir / f"{pdf_path.stem}.tables.legend.csv"
        with legend_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "page_number",
                    "index_on_page",
                    "engine",
                    "rows",
                    "csv",
                    "json",
                ],
            )
            writer.writeheader()
            writer.writerows(legend_entries)

    markdown = build_markdown_document(pdf_path.name, result.pages, manifest)
    md_path = outdir / f"{pdf_path.stem}.extracted.md"
    write_text(md_path, markdown)

    html_doc = build_html_document(pdf_path.name, result.pages, manifest)
    html_path = outdir / f"{pdf_path.stem}.extracted.html"
    write_text(html_path, html_doc)

    quality_json_path: Optional[Path] = None
    quality_md_path: Optional[Path] = None
    if result.quality_report:
        quality_json_path = outdir / f"{pdf_path.stem}.quality.json"
        write_text(quality_json_path, json.dumps(result.quality_report, indent=2))
    if result.quality_markdown:
        quality_md_path = outdir / f"{pdf_path.stem}.quality.md"
        write_text(quality_md_path, result.quality_markdown)

    artefacts: Dict[str, object] = {
        "markdown": md_path,
        "html": html_path,
        "tables_legend": legend_path,
        "quality_json": quality_json_path,
        "quality_markdown": quality_md_path,
        "table_csvs": table_csv_paths,
        "table_jsons": table_json_paths,
    }
    return artefacts


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pipeline",
        description="Run the PDF processing pipeline and optionally persist artefacts.",
    )
    parser.add_argument("pdf", type=Path, help="Path to the PDF file to process.")
    parser.add_argument(
        "-o",
        "--outdir",
        type=Path,
        help="Directory where markdown, HTML, and table CSV/JSON files will be written.",
    )
    return parser.parse_args(argv)


def main(pdf_path: Path, outdir: Optional[Path] = None) -> None:
    logging.basicConfig(level=logging.DEBUG)
    pipeline = PDFProcessingPipeline(PipelineConfig())
    result = pipeline.run(pdf_path)

    print(f"Text engine: {result.text_engine}")
    print(f"OCR strategy: {result.ocr_strategy}")
    print(f"Tables detected: {len(result.tables)}")
    if result.metrics:
        print("Metrics:")
        for key, value in asdict(result.metrics).items():
            print(f"  {key}: {value}")

    if outdir:
        artefacts = _persist_outputs(pdf_path, result, outdir)
        for label, value in artefacts.items():
            if not value:
                continue
            if isinstance(value, list):
                for index, item in enumerate(value, start=1):
                    print(f"{label}[{index}]: {item}")
            else:
                print(f"{label}: {value}")

    if result.markdown:
        print("\n--- Extracted Markdown ---\n")
        print(result.markdown)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    pdf_arg = args.pdf.expanduser()
    if not pdf_arg.exists():
        raise SystemExit(f"Missing PDF: {pdf_arg}")
    output_dir = args.outdir.expanduser() if args.outdir else None
    main(pdf_arg, output_dir)
