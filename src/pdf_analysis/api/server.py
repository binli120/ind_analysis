# @author: Bin Lee
# @email: blee@filynai.com

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, cast

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, UploadFile

from pdf_analysis.ingest.pdf_text import extract_pages_text
from pdf_analysis.ingest.tables import extract_tables_all
from pdf_analysis.transform.markdown_writer import (
    build_html_document,
    build_markdown_document,
)
from pdf_analysis.validate import generate_quality_report

app = FastAPI(
    title="PDF Analysis API",
    description="Upload a PDF study report and receive extracted content, structured tables, and quality analysis.",
    version="0.1.0",
)


def _table_to_payload(
    table: Dict[str, Any], *, max_rows: Optional[int] = None
) -> Dict[str, Any]:
    df: pd.DataFrame = table["dataframe"].fillna("").astype(str)
    if max_rows is not None:
        df_preview = df.head(max_rows)
    else:
        df_preview = df
    return {
        "page_number": table["page_number"],
        "index_on_page": table["index_on_page"],
        "engine": table["engine"],
        "columns": [str(col) for col in df.columns],
        "rows": df_preview.to_dict(orient="records"),
        "row_count": int(df.shape[0]),
    }


def _build_table_manifest(
    tables: List[Dict[str, Any]], preview_rows: int = 10
) -> List[Dict[str, Any]]:
    manifest: List[Dict[str, Any]] = []
    for table in tables:
        df: pd.DataFrame = table["dataframe"].fillna("").astype(str)
        manifest.append(
            {
                "page_number": table["page_number"],
                "index_on_page": table["index_on_page"],
                "engine": table["engine"],
                "csv": None,
                "json": None,
                "preview_rows": df.head(preview_rows),
            }
        )
    return manifest


@app.post("/analyze")
async def analyze_pdf(
    file: UploadFile = File(...),
    engine: str = Query(
        "pdfplumber",
        description="Table extraction engine to use.",
        pattern="^(pdfplumber|camelot|tabula)$",
    ),
    max_pages: Optional[int] = Query(
        None,
        ge=1,
        description="Limit the number of pages to process (useful for quick tests).",
    ),
    ocr_fallback: bool = Query(
        False,
        description="Attempt OCR on pages that yield no text (requires pytesseract/pdf2image).",
    ),
    table_rows: Optional[int] = Query(
        200,
        ge=1,
        description="Maximum number of rows to include per table in the response (set to null for all rows).",
    ),
) -> Dict[str, Any]:
    """
    Analyze an uploaded PDF and return the extracted content and diagnostics.
    """
    filename = file.filename or "uploaded.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    with NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp_path = Path(tmp.name)
        try:
            shutil.copyfileobj(file.file, tmp)
        finally:
            file.file.close()

    try:
        return await asyncio.to_thread(
            _run_pipeline_with_runner,
            tmp_path,
            filename,
            max_pages,
            engine,
            ocr_fallback,
            table_rows,
        )
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


def _run_pipeline_with_runner(
    pdf_path: Path,
    filename: str,
    max_pages: Optional[int],
    table_engine: str,
    ocr_fallback: bool,
    table_rows: Optional[int],
) -> Dict[str, Any]:
    pages = extract_pages_text(
        pdf_path,
        ocr_fallback=ocr_fallback,
        max_pages=max_pages,
    )

    tables = extract_tables_all(
        pdf_path,
        engine=table_engine,
        max_pages=max_pages,
    )

    table_manifest = _build_table_manifest(tables)
    markdown = build_markdown_document(filename, pages, table_manifest)
    html = build_html_document(filename, pages, table_manifest)

    quality = generate_quality_report(
        pdf_path,
        pages,
        tables,
        extraction_limit=max_pages,
    )

    tables_payload = [_table_to_payload(t, max_rows=table_rows) for t in tables]

    metrics_payload = {
        "total_pages": len(pages),
        "pages_with_text": sum(
            1 for page in pages if (page.get("text") or "").strip()
        ),
        "tables_total": len(tables),
        "table_pages": len(
            {table["page_number"] for table in tables if table.get("page_number")}
        ),
        "ocr_pages": 0,
        "key_value_pairs": 0,
        "text_coverage": 0.0,
        "confidence": None,
    }
    total_pages = metrics_payload["total_pages"]
    if total_pages:
        metrics_payload["text_coverage"] = round(
            float(cast(float, metrics_payload["pages_with_text"])) /
            float(cast(float, total_pages)),
            3,
        )

    return {
        "document": filename,
        "pages": pages,
        "markdown": markdown,
        "html": html,
        "tables": tables_payload,
        "quality": quality.get("json", {}) if quality else {},
        "quality_markdown": quality.get("markdown") if quality else None,
        "metrics": metrics_payload,
    }

def export_openapi_to_file(app: FastAPI, out_path: str | Path) -> None:
    """Write the OpenAPI spec to JSON (or YAML)."""
    openapi_dict = app.openapi()
    out_path = Path(out_path)

    # JSON
    if out_path.suffix in {".json", ".yml", ".yaml"}:
        data = json.dumps(openapi_dict, indent=2)
    else:
        raise ValueError("Supported extensions: .json, .yml, .yaml")

    out_path.write_text(data, encoding="utf-8")
    print(f"✅ OpenAPI spec written to {out_path}")