# @author: Bin Lee
# @email: blee@filynai.com

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, UploadFile

from pdf_analysis.pipeline import PipelineConfig, PipelineRunner

app = FastAPI(
    title="PDF Analysis API",
    description="Upload a PDF study report and receive extracted content, structured tables, and quality analysis.",
    version="0.1.0",
)


_PIPELINE_MAX_WORKERS_ENV = os.getenv("PDF_PIPELINE_MAX_WORKERS")
_PIPELINE_MAX_WORKERS: Optional[int]
if _PIPELINE_MAX_WORKERS_ENV and _PIPELINE_MAX_WORKERS_ENV.isdigit():
    _PIPELINE_MAX_WORKERS = int(_PIPELINE_MAX_WORKERS_ENV)
else:
    _PIPELINE_MAX_WORKERS = None


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
    config = PipelineConfig()
    config.text.max_pages = max_pages
    config.ocr.enable = ocr_fallback
    config.structured.table_engines = (table_engine,)

    runner = PipelineRunner(config=config, max_workers=_PIPELINE_MAX_WORKERS)
    tasks = runner.run_many([pdf_path])
    if not tasks:
        raise HTTPException(status_code=500, detail="Pipeline runner returned no results.")

    task = tasks[0]
    if task.error or task.result is None:
        error = task.error or RuntimeError("Pipeline returned no result")
        if isinstance(error, HTTPException):
            raise error
        raise HTTPException(status_code=500, detail=str(error)) from error

    result = task.result

    tables_payload = [_table_to_payload(t, max_rows=table_rows) for t in result.tables]

    return {
        "document": filename,
        "pages": result.pages,
        "markdown": result.markdown,
        "html": result.html,
        "tables": tables_payload,
        "quality": result.quality_report or {},
        "quality_markdown": result.quality_markdown,
    }
