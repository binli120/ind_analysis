from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pdf_analysis.ingest.pdf_text import extract_pages_text
from pdf_analysis.ingest.tables import extract_tables_all
from pdf_analysis.transform.markdown_writer import (
    build_html_document,
    build_markdown_document,
)
from pdf_analysis.validate import generate_quality_report

from .config import PipelineConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PipelineContext:
    pdf_path: Path
    pages: List[Dict[str, Any]] = field(default_factory=list)
    text_engine: Optional[str] = None
    ocr_strategy: Optional[str] = None
    tables: List[Dict[str, Any]] = field(default_factory=list)
    table_engines: List[str] = field(default_factory=list)
    key_values: Dict[str, str] = field(default_factory=dict)
    extras: Dict[str, Any] = field(default_factory=dict)

    def full_text(self) -> str:
        return "\n\n".join(page.get("text", "") for page in self.pages if page.get("text"))


@dataclass(slots=True)
class PipelineResult:
    pages: List[Dict[str, Any]]
    tables: List[Dict[str, Any]]
    key_values: Dict[str, str]
    markdown: Optional[str]
    html: Optional[str]
    quality_report: Optional[Dict[str, Any]]
    quality_markdown: Optional[str]
    text_engine: Optional[str]
    ocr_strategy: Optional[str]
    table_engines: Sequence[str]
    langchain_output: Optional[Any]
    extras: Dict[str, Any] = field(default_factory=dict)


class PDFProcessingPipeline:
    """Runs staged PDF extraction with graceful fallbacks."""

    def __init__(self, config: Optional[PipelineConfig] = None) -> None:
        self.config = config or PipelineConfig()

    # ------------------------------------------------------------------
    def run(self, pdf_path: Path) -> PipelineResult:
        intermediates: Dict[str, Any] = {}
        ctx = PipelineContext(pdf_path=pdf_path, extras=intermediates)

        logger.info("[pipeline] starting processing for %s", pdf_path.name)

        ctx.pages, ctx.text_engine = self._run_text_extraction(pdf_path, ctx.extras)
        logger.debug(
            "[pipeline] text extraction engine=%s pages=%d",
            ctx.text_engine,
            len(ctx.pages),
        )
        if self.config.ocr.enable:
            self._run_ocr_stage(ctx)
            logger.debug("[pipeline] OCR strategy=%s", ctx.ocr_strategy)

        ctx.tables, ctx.table_engines = self._run_structured_stage(pdf_path)
        logger.debug(
            "[pipeline] structured extraction engines=%s tables=%d",
            ctx.table_engines,
            len(ctx.tables),
        )
        ctx.key_values = self._run_key_value_stage(ctx)
        logger.debug(
            "[pipeline] extracted key-values count=%d", len(ctx.key_values)
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
            "[pipeline] completed processing for %s (pages=%d tables=%d)",
            pdf_path.name,
            len(ctx.pages),
            len(ctx.tables),
        )

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
        )

    # ------------------------------------------------------------------
    def _run_text_extraction(
        self, pdf_path: Path, extras: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        engines = self.config.text.engines
        max_pages = self.config.text.max_pages
        for engine in engines:
            try:
                pages = self._extract_text_with_engine(pdf_path, engine, max_pages)
            except ModuleNotFoundError as exc:
                self._handle_missing_dependency(engine, exc)
                continue
            except Exception as exc:  # pragma: no cover - protective
                logger.warning("%s text extraction failed: %s", engine, exc)
                continue

            if self._has_text_payload(pages):
                return pages, engine
            if self.config.text.keep_intermediate:
                extras[f"text_{engine}"] = [p.copy() for p in pages]
        return [], None

    def _extract_text_with_engine(
        self, pdf_path: Path, engine: str, max_pages: Optional[int]
    ) -> List[Dict[str, Any]]:
        if engine == "pdfminer":
            return extract_pages_text(pdf_path, max_pages=max_pages)
        if engine == "pdfplumber":
            pdfplumber = import_module("pdfplumber")
            pages: List[Dict[str, Any]] = []
            with pdfplumber.open(str(pdf_path)) as pdf:
                limit = min(len(pdf.pages), max_pages) if max_pages else len(pdf.pages)
                for idx in range(limit):
                    page = pdf.pages[idx]
                    text = page.extract_text(layout=True) or ""
                    pages.append({"page_number": idx + 1, "text": text.strip()})
            return pages
        if engine in {"pymupdf", "fitz"}:
            fitz = import_module("fitz")
            doc = fitz.open(str(pdf_path))
            pages: List[Dict[str, Any]] = []
            limit = min(len(doc), max_pages) if max_pages else len(doc)
            for idx in range(limit):
                page = doc.load_page(idx)
                text = page.get_text("text")
                pages.append({"page_number": idx + 1, "text": text.strip()})
            return pages
        raise ValueError(f"Unknown text engine: {engine}")

    def _has_text_payload(self, pages: List[Dict[str, Any]]) -> bool:
        return any((page.get("text") or "").strip() for page in pages)

    # ------------------------------------------------------------------
    def _run_ocr_stage(self, ctx: PipelineContext) -> None:
        empty_ids = [idx for idx, page in enumerate(ctx.pages) if not page.get("text")]
        if not empty_ids:
            return

        strategy = (self.config.ocr.strategy or "tesseract").lower()
        texts: Dict[int, str] = {}

        if strategy == "tesseract":
            from pdf_analysis.ingest.ocr import ocr_pages_if_needed

            texts = ocr_pages_if_needed(
                ctx.pdf_path, empty_ids, lang=self.config.ocr.languages
            )
        elif strategy == "textract":
            client = self.config.ocr.textract_client
            if client is None:
                self._handle_missing_client("textract", "Provide boto3 Textract client callable")
            else:
                texts = self._invoke_callable_client(client, ctx.pdf_path, empty_ids)
        elif strategy in {"gcv", "vision"}:
            client = self.config.ocr.gcv_client
            if client is None:
                self._handle_missing_client("google-vision", "Provide Vision API callable")
            else:
                texts = self._invoke_callable_client(client, ctx.pdf_path, empty_ids)
        else:
            logger.warning("Unknown OCR strategy %s", strategy)

        for idx, text in texts.items():
            if idx < len(ctx.pages) and text:
                ctx.pages[idx]["text"] = text.strip()
        if texts:
            ctx.ocr_strategy = strategy
            logger.debug(
                "[pipeline] OCR recovered text for pages=%s",
                sorted(texts.keys()),
            )

    def _invoke_callable_client(
        self, client: Any, pdf_path: Path, empty_ids: Sequence[int]
    ) -> Dict[int, str]:
        if hasattr(client, "__call__"):
            return client(pdf_path, empty_ids)  # type: ignore[return-value]
        raise TypeError("Client must be callable and accept (pdf_path, page_ids)")

    def _handle_missing_client(self, name: str, hint: str) -> None:
        message = f"No client configured for {name} OCR stage. {hint}."
        if self.config.raise_on_missing_dependencies:
            raise RuntimeError(message)
        logger.warning(message)

    # ------------------------------------------------------------------
    def _run_structured_stage(
        self, pdf_path: Path
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        tables: List[Dict[str, Any]] = []
        engines_used: List[str] = []
        for engine in self.config.structured.table_engines:
            try:
                items = extract_tables_all(
                    pdf_path,
                    engine=engine,
                    max_pages=self.config.text.max_pages,
                )
            except RuntimeError as exc:
                self._handle_missing_dependency(engine, exc)
                continue
            except ModuleNotFoundError as exc:  # pragma: no cover - defensive
                self._handle_missing_dependency(engine, exc)
                continue
            if items:
                tables.extend(items)
                engines_used.append(engine)
                logger.debug(
                    "[pipeline] engine %s yielded %d tables",
                    engine,
                    len(items),
                )
        return tables, engines_used

    def _run_key_value_stage(self, ctx: PipelineContext) -> Dict[str, str]:
        patterns = self.config.structured.key_value_patterns
        if not patterns:
            return {}
        text = ctx.full_text()
        output: Dict[str, str] = {}
        for label, pattern in patterns.items():
            match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
            if match:
                output[label] = match.group(1 if match.lastindex else 0).strip()
        return output

    # ------------------------------------------------------------------
    def _build_editor_artifacts(
        self, ctx: PipelineContext
    ) -> Tuple[Optional[str], Optional[str]]:
        if not ctx.pages:
            return None, None
        try:
            markdown = build_markdown_document(
                ctx.pdf_path.name,
                ctx.pages,
                self._make_table_preview(ctx.tables),
            )
            html = build_html_document(
                ctx.pdf_path.name,
                ctx.pages,
                self._make_table_preview(ctx.tables),
            )
            logger.debug(
                "[pipeline] built markdown length=%d html length=%d",
                len(markdown) if markdown else 0,
                len(html) if html else 0,
            )
            return markdown, html
        except Exception as exc:  # pragma: no cover - formatting stage
            logger.warning("Failed to build markdown/HTML artifacts: %s", exc)
            return None, None

    def _make_table_preview(self, tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        preview: List[Dict[str, Any]] = []
        for table in tables:
            df = table.get("dataframe")
            if df is None:
                continue
            preview.append(
                {
                    "page_number": table.get("page_number"),
                    "index_on_page": table.get("index_on_page"),
                    "engine": table.get("engine"),
                    "preview_rows": df.head(10),
                    "csv": None,
                    "json": None,
                }
            )
        return preview

    def _generate_quality(self, ctx: PipelineContext) -> Optional[Dict[str, Any]]:
        if not ctx.pages and not ctx.tables:
            return None
        try:
            return generate_quality_report(
                ctx.pdf_path,
                ctx.pages,
                ctx.tables,
                extraction_limit=self.config.text.max_pages,
            )
        except Exception as exc:  # pragma: no cover - optional stage
            logger.warning("Quality report generation failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    def _run_langchain_stage(self, ctx: PipelineContext) -> Optional[Any]:
        if not self.config.llm.enabled:
            return None

        try:
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_text_splitters import RecursiveCharacterTextSplitter
            from langchain_core.documents import Document
        except ModuleNotFoundError as exc:
            self._handle_missing_dependency("langchain", exc)
            return None

        documents = [
            Document(page_content=ctx.full_text(), metadata={"source": ctx.pdf_path.name})
        ]
        if ctx.tables:
            table_text = []
            for table in ctx.tables:
                df = table.get("dataframe")
                if df is not None:
                    table_text.append(df.to_csv(index=False))
            if table_text:
                documents.append(
                    Document(
                        page_content="\n\n".join(table_text),
                        metadata={"source": f"{ctx.pdf_path.name}-tables"},
                    )
                )

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.config.llm.chunk_size,
            chunk_overlap=self.config.llm.chunk_overlap,
        )
        splits = splitter.split_documents(documents)

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    self.config.llm.prompt_template,
                ),
                (
                    "human",
                    "Context:\n{context}\n\nReturn format: {output_format}",
                ),
            ]
        )

        chain = prompt | self._create_llm()
        contexts = [split.page_content for split in splits]
        merged_context = "\n\n".join(contexts)
        return chain.invoke(
            {
                "context": merged_context,
                "output_format": self.config.llm.output_format,
            }
        )

    def _create_llm(self):
        if self.config.llm.factory is not None:
            return self.config.llm.factory(self.config.llm)

        provider = (self.config.llm.provider or "openai").lower()
        if provider == "openai":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=self.config.llm.model,
                temperature=self.config.llm.temperature,
            )
        if provider in {"anthropic", "claude"}:
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(
                model=self.config.llm.model,
                temperature=self.config.llm.temperature,
            )
        raise ValueError(f"Unsupported LangChain provider: {provider}")

    # ------------------------------------------------------------------
    def _handle_missing_dependency(self, name: str, exc: Exception) -> None:
        message = f"Optional dependency for {name} not available: {exc}"
        if self.config.raise_on_missing_dependencies:
            raise ModuleNotFoundError(message) from exc
        logger.warning(message)
