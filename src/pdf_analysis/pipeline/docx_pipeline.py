# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""DOCX processing pipeline with table + text extraction."""

# @author: Bin Lee
# @email: blee@longooc.com

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from pdf_analysis.ingest.docx_text import extract_docx_pages, extract_docx_tables

from .pipeline import PDFProcessingPipeline, PipelineContext, PipelineResult

logger = logging.getLogger(__name__)


class DocxProcessingPipeline(PDFProcessingPipeline):
    """Runs staged DOCX extraction using python-docx."""

    def run(self, docx_path: Path) -> PipelineResult:
        intermediates: Dict[str, Any] = {}
        ctx = PipelineContext(pdf_path=docx_path, extras=intermediates)

        logger.info("[pipeline] starting DOCX processing for %s", docx_path.name)

        ctx.pages = extract_docx_pages(
            docx_path, max_pages=self.config.text.max_pages
        )
        ctx.text_engine = "docx"
        if self.config.structured.table_engines:
            ctx.tables = extract_docx_tables(docx_path)
            ctx.table_engines = ["docx"] if ctx.tables else []
        else:
            ctx.tables = []
            ctx.table_engines = []
        ctx.key_values = self._run_key_value_stage(ctx)
        logger.debug(
            "[pipeline] docx extraction pages=%d tables=%d",
            len(ctx.pages),
            len(ctx.tables),
        )

        markdown, html = self._build_editor_artifacts(ctx)
        quality = self._generate_quality(ctx)
        logger.debug(
            "[pipeline] artifacts markdown=%s html=%s quality=%s",
            bool(markdown),
            bool(html),
            bool(quality),
        )

        langchain_output = self._run_langchain_stage(ctx)
        logger.debug(
            "[pipeline] langchain output present=%s",
            langchain_output is not None,
        )

        logger.info(
            "[pipeline] completed DOCX processing for %s (pages=%d tables=%d)",
            docx_path.name,
            len(ctx.pages),
            len(ctx.tables),
        )
        metrics = self._compute_metrics(ctx, quality)

        return PipelineResult(
            pages=ctx.pages,
            tables=ctx.tables,
            key_values=ctx.key_values,
            markdown=markdown,
            html=html,
            quality_report=quality["json"] if quality else None,
            quality_markdown=quality["markdown"] if quality else None,
            text_engine=ctx.text_engine,
            ocr_strategy=ctx.ocr_strategy,
            table_engines=tuple(ctx.table_engines),
            langchain_output=langchain_output,
            extras=ctx.extras,
            metrics=metrics,
        )
