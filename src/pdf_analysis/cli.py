# @author: Bin Lee
# @email: blee@filynai.com

import argparse
import json
from pathlib import Path

from pdf_analysis.export.persist import save_tables, write_text
from pdf_analysis.export.pdf_writer import markdown_to_pdf
from pdf_analysis.ingest.pdf_text import extract_pages_text
from pdf_analysis.ingest.tables import extract_tables_all
from pdf_analysis.transform.markdown_writer import (
    build_html_document,
    build_markdown_document,
)
from pdf_analysis.validate import generate_quality_report


def main():
    ap = argparse.ArgumentParser(
        prog="ind-x", description="PDF → Editable Markdown + Tables"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser(
        "extract", help="Extract text + tables and emit Markdown + CSV/JSON"
    )
    sp.add_argument("pdf", nargs="+", help="PDF file(s)")
    sp.add_argument("-o", "--outdir", required=True, help="Output directory")
    sp.add_argument(
        "--ocr-fallback", action="store_true", help="Enable OCR if a page has no text"
    )
    sp.add_argument(
        "--engine",
        choices=["pdfplumber", "camelot", "tabula"],
        default="pdfplumber",
        help="Table engine (default: pdfplumber)",
    )
    sp.add_argument(
        "--max-pages", type=int, default=None, help="Limit page count for testing"
    )
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    for pdf_path in args.pdf:
        pdf = Path(pdf_path)
        if not pdf.exists():
            print(f"[WARN] Missing: {pdf}")
            continue

        # 1) Page text (optionally OCR)
        pages = extract_pages_text(
            pdf, ocr_fallback=args.ocr_fallback, max_pages=args.max_pages
        )

        # 2) Tables (dataframes + metadata)
        tables = extract_tables_all(pdf, engine=args.engine, max_pages=args.max_pages)

        # 3) Persist tables
        table_manifest = save_tables(pdf, tables, outdir)

        # 4) Build Markdown that includes BOTH text and tables
        md = build_markdown_document(pdf.name, pages, table_manifest)
        md_path = outdir / f"{pdf.stem}.extracted.md"

        if md_path.exists():
            existing_md = md_path.read_text(encoding="utf-8")
            if existing_md != md:
                modified_pdf_path = outdir / f"{pdf.stem}.modified.pdf"
                markdown_to_pdf(existing_md, modified_pdf_path)
                print(f"[INFO] Preserved modified draft → {modified_pdf_path}")

        write_text(md_path, md)

        html_doc = build_html_document(pdf.name, pages, table_manifest)
        html_path = outdir / f"{pdf.stem}.extracted.html"
        write_text(html_path, html_doc)

        quality = generate_quality_report(
            pdf,
            pages,
            tables,
            extraction_limit=args.max_pages,
        )
        quality_json_path = outdir / f"{pdf.stem}.quality.json"
        quality_md_path = outdir / f"{pdf.stem}.quality.md"
        write_text(quality_json_path, json.dumps(quality["json"], indent=2))
        write_text(quality_md_path, quality["markdown"])

        print(f"[OK] {pdf.name} → {md_path}")
        print(f"[OK] {pdf.name} → {html_path}")
        print(f"[OK] {pdf.name} → {quality_json_path}")
        print(f"[OK] {pdf.name} → {quality_md_path}")


if __name__ == "__main__":
    main()
