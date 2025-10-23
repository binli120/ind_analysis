# @author: Bin Lee
# @email: blee@filynai.com

from __future__ import annotations

import itertools
import json
import logging
import re
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from pdf_analysis.ingest.pdf_text import extract_pages_text, iter_pages_text
from pdf_analysis.ingest.tables import extract_tables, extract_tables_all
from pdf_analysis.transform.markdown_writer import (
    build_html_document,
    build_markdown_document,
)
from pdf_analysis.validate import generate_quality_report

from .config import PipelineConfig, RedisStreamingConfig

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
    ocr_pages: set[int] = field(default_factory=set)

    def full_text(self) -> str:
        return "\n\n".join(
            page.get("text", "") for page in self.pages if page.get("text")
        )


@dataclass(slots=True)
class PipelineMetrics:
    total_pages: int
    pages_with_text: int
    text_coverage: float
    ocr_pages: int
    tables_total: int
    table_pages: int
    key_value_pairs: int
    confidence: float


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
    metrics: PipelineMetrics | None = None


@dataclass(slots=True)
class PipelineChunk:
    chunk_index: int
    pages: List[Dict[str, Any]]
    tables: List[Dict[str, Any]]
    ocr_pages: List[int]
    text_engine: Optional[str]
    ocr_strategy: Optional[str]
    table_engines: Sequence[str]

    def to_serializable(self) -> Dict[str, Any]:
        """
        Convert the chunk into a JSON-friendly payload. DataFrames are materialised
        to records to avoid pickling when persisting to external stores.
        """
        tables_payload: List[Dict[str, Any]] = []
        for table in self.tables:
            df = table.get("dataframe")
            tables_payload.append(
                {
                    "page_number": table.get("page_number"),
                    "index_on_page": table.get("index_on_page"),
                    "engine": table.get("engine"),
                    "columns": list(df.columns) if df is not None else [],
                    "rows": df.to_dict(orient="records") if df is not None else [],
                }
            )

        return {
            "chunk_index": self.chunk_index,
            "pages": [page.copy() for page in self.pages],
            "ocr_pages": sorted(self.ocr_pages),
            "text_engine": self.text_engine,
            "ocr_strategy": self.ocr_strategy,
            "table_engines": list(self.table_engines),
            "tables": tables_payload,
        }


class PDFProcessingPipeline:
    """Runs staged PDF extraction with graceful fallbacks."""

    def __init__(self, config: Optional[PipelineConfig] = None) -> None:
        self.config = config or PipelineConfig()
        self._pdfplumber_text_supported = True
        self._last_stream_extras: Optional[Dict[str, Any]] = None
        self._redis_client_cache: Optional[Any] = None

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
        logger.debug("[pipeline] extracted key-values count=%d", len(ctx.key_values))

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

    def stream(
        self,
        pdf_path: Path,
        *,
        chunk_size: int = 1,
        redis_client: Any | None = None,
        redis_key: Optional[str] = None,
        redis_expire: Optional[int] = None,
        serializer: Optional[Callable[[PipelineChunk], Any]] = None,
    ) -> Iterator[PipelineChunk]:
        """
        Lazily processes the PDF and yields chunked page/table payloads.
        When a redis client is provided, each chunk is persisted immediately using
        HSET (field format: ``chunk:{index:05d}``).
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        redis_config = getattr(self.config, "redis", None)

        if redis_client is None and isinstance(redis_config, RedisStreamingConfig) and redis_config.enabled:
            redis_client = self._get_redis_client(redis_config)
            if redis_client is None:
                logger.warning("[pipeline] redis streaming disabled due to missing client")
        if redis_client is not None and redis_key is None and isinstance(redis_config, RedisStreamingConfig) and redis_config.enabled:
            redis_key = redis_config.key_template.format(
                filename=pdf_path.name,
                stem=pdf_path.stem,
            )
            if redis_expire is None:
                redis_expire = redis_config.expire_seconds

        if redis_client is not None and not redis_key:
            raise ValueError("redis_key is required when redis_client is provided")
        if redis_client is None and redis_key is not None:
            raise ValueError("redis_client must be provided when redis_key is set")

        extras: Dict[str, Any] = {}
        iterator, text_engine = self._select_text_stream(pdf_path, extras)
        self._last_stream_extras = extras
        if text_engine is None:
            logger.warning(
                "[pipeline] no text engine produced text for %s", pdf_path.name
            )
        logger.info(
            "[pipeline] streaming processing for %s (chunk_size=%d)",
            pdf_path.name,
            chunk_size,
        )

        chunk_index = 0
        chunk_pages: List[Dict[str, str]] = []
        chunk_page_numbers: List[int] = []
        pending_ocr: List[int] = []
        chunk_ocr_pages: set[int] = set()

        for page in iterator:
            page_number = page.get("page_number")
            if page_number is None:
                continue
            text = page.get("text", "")
            page_entry = {"page_number": page_number, "text": text}

            chunk_pages.append(page_entry)
            chunk_page_numbers.append(page_number)
            if self.config.ocr.enable and not (text or "").strip():
                pending_ocr.append(page_number)

            if len(chunk_pages) == chunk_size:
                chunk_index += 1
                chunk = self._finalise_chunk(
                    pdf_path,
                    chunk_index,
                    chunk_pages,
                    chunk_page_numbers,
                    pending_ocr,
                    chunk_ocr_pages,
                    text_engine,
                    redis_client,
                    redis_key,
                    serializer,
                )
                yield chunk

                chunk_pages = []
                chunk_page_numbers = []
                pending_ocr = []
                chunk_ocr_pages = set()

        # Flush final chunk if there are remaining pages
        if chunk_pages:
            chunk_index += 1
            chunk = self._finalise_chunk(
                pdf_path,
                chunk_index,
                chunk_pages,
                chunk_page_numbers,
                pending_ocr,
                chunk_ocr_pages,
                text_engine,
                redis_client,
                redis_key,
                serializer,
            )
            yield chunk

        if redis_client is not None and redis_key:
            redis_client.hset(redis_key, "status", "complete")
            redis_client.hset(redis_key, "chunks", chunk_index)
            if text_engine:
                redis_client.hset(redis_key, "text_engine", text_engine)
            if redis_expire is not None:
                redis_client.expire(redis_key, int(redis_expire))

        logger.info(
            "[pipeline] streaming completed for %s (chunks=%d)",
            pdf_path.name,
            chunk_index,
        )

    def _finalise_chunk(
        self,
        pdf_path: Path,
        chunk_index: int,
        chunk_pages: List[Dict[str, str]],
        chunk_page_numbers: List[int],
        pending_ocr: List[int],
        chunk_ocr_pages: set[int],
        text_engine: Optional[str],
        redis_client: Any | None,
        redis_key: Optional[str],
        serializer: Optional[Callable[[PipelineChunk], Any]],
    ) -> PipelineChunk:
        ocr_strategy: Optional[str] = None
        if pending_ocr:
            ocr_result, strategy = self._run_ocr_for_page_numbers(pdf_path, pending_ocr)
            if ocr_result:
                ocr_strategy = strategy
                for page in chunk_pages:
                    page_no = page["page_number"]
                    if page_no in ocr_result:
                        page["text"] = ocr_result[page_no]
                        chunk_ocr_pages.add(page_no)

        tables, engines_used = self._extract_tables_for_pages(pdf_path, chunk_page_numbers)
        chunk = PipelineChunk(
            chunk_index=chunk_index,
            pages=[page.copy() for page in chunk_pages],
            tables=tables,
            ocr_pages=sorted(chunk_ocr_pages),
            text_engine=text_engine,
            ocr_strategy=ocr_strategy,
            table_engines=tuple(engines_used),
        )

        if redis_client is not None and redis_key:
            self._persist_chunk_to_redis(redis_client, redis_key, chunk, serializer)

        return chunk

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
            if not self._pdfplumber_text_supported:
                raise RuntimeError("pdfplumber text engine disabled due to compatibility issue")
            self._patch_pdfminer_for_pdfplumber()
            pdfplumber = import_module("pdfplumber")
            pages: List[Dict[str, Any]] = []
            try:
                with pdfplumber.open(str(pdf_path)) as pdf:
                    limit = min(len(pdf.pages), max_pages) if max_pages else len(pdf.pages)
                    for idx in range(limit):
                        page = pdf.pages[idx]
                        try:
                            text = page.extract_text(layout=True) or ""
                        except AttributeError as exc:
                            if "graphicstate" in str(exc):
                                self._pdfplumber_text_supported = False
                                logger.warning(
                                    "pdfplumber text extraction disabled: %s", exc
                                )
                                return []
                            raise
                        pages.append({"page_number": idx + 1, "text": text.strip()})
            except AttributeError as exc:
                if "graphicstate" in str(exc):
                    self._pdfplumber_text_supported = False
                    logger.warning("pdfplumber text extraction disabled: %s", exc)
                    return []
                raise
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

    def _iter_text_with_engine(
        self,
        pdf_path: Path,
        engine: str,
        max_pages: Optional[int],
    ) -> Iterator[Dict[str, str]]:
        if engine == "pdfminer":
            yield from iter_pages_text(pdf_path, max_pages=max_pages)
            return

        if engine == "pdfplumber":
            if not self._pdfplumber_text_supported:
                raise RuntimeError(
                    "pdfplumber text engine disabled due to compatibility issue"
                )
            self._patch_pdfminer_for_pdfplumber()
            pdfplumber = import_module("pdfplumber")

            def generator() -> Iterator[Dict[str, str]]:
                with pdfplumber.open(str(pdf_path)) as pdf:
                    limit = min(len(pdf.pages), max_pages) if max_pages else len(pdf.pages)
                    for idx in range(limit):
                        page = pdf.pages[idx]
                        try:
                            text = page.extract_text(layout=True) or ""
                        except AttributeError as exc:
                            if "graphicstate" in str(exc):
                                self._pdfplumber_text_supported = False
                                logger.warning(
                                    "pdfplumber text extraction disabled: %s",
                                    exc,
                                )
                                return
                            raise
                        yield {"page_number": idx + 1, "text": text.strip()}

            return generator()

        if engine in {"pymupdf", "fitz"}:
            fitz = import_module("fitz")

            def generator() -> Iterator[Dict[str, str]]:
                doc = fitz.open(str(pdf_path))
                try:
                    limit = min(len(doc), max_pages) if max_pages else len(doc)
                    for idx in range(limit):
                        page = doc.load_page(idx)
                        text = page.get_text("text")
                        yield {"page_number": idx + 1, "text": text.strip()}
                finally:
                    doc.close()

            return generator()

        raise ValueError(f"Unknown text engine: {engine}")

    def _has_text_payload(self, pages: List[Dict[str, Any]]) -> bool:
        return any((page.get("text") or "").strip() for page in pages)

    def _select_text_stream(
        self, pdf_path: Path, extras: Dict[str, Any]
    ) -> Tuple[Iterator[Dict[str, str]], Optional[str]]:
        engines = self.config.text.engines
        max_pages = self.config.text.max_pages
        probe = max(1, int(self.config.text.stream_probe_pages))

        for engine in engines:
            try:
                iterator = self._iter_text_with_engine(pdf_path, engine, max_pages)
            except ModuleNotFoundError as exc:
                self._handle_missing_dependency(engine, exc)
                continue
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("%s text extraction failed: %s", engine, exc)
                continue

            buffer: List[Dict[str, str]] = []
            has_text = False
            try:
                for page in itertools.islice(iterator, probe):
                    buffer.append(page)
                    if (page.get("text") or "").strip():
                        has_text = True
                if has_text or not buffer:
                    return itertools.chain(buffer, iterator), engine
                if self.config.text.keep_intermediate and buffer:
                    extras[f"text_{engine}"] = [p.copy() for p in buffer]
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("%s text streaming failed mid-run: %s", engine, exc)
            finally:
                if not has_text:
                    self._close_iterator(iterator)
        return iter(()), None

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
                self._handle_missing_client(
                    "textract", "Provide boto3 Textract client callable"
                )
            else:
                texts = self._invoke_callable_client(client, ctx.pdf_path, empty_ids)
        elif strategy in {"gcv", "vision"}:
            client = self.config.ocr.gcv_client
            if client is None:
                self._handle_missing_client(
                    "google-vision", "Provide Vision API callable"
                )
            else:
                texts = self._invoke_callable_client(client, ctx.pdf_path, empty_ids)
        else:
            logger.warning("Unknown OCR strategy %s", strategy)

        for idx, text in texts.items():
            if idx < len(ctx.pages) and text:
                ctx.pages[idx]["text"] = text.strip()
                ctx.ocr_pages.add(idx + 1)
        if texts:
            ctx.ocr_strategy = strategy
            logger.debug(
                "[pipeline] OCR recovered text for pages=%s",
                sorted(texts.keys()),
            )

    def _run_ocr_for_page_numbers(
        self, pdf_path: Path, page_numbers: Sequence[int]
    ) -> Tuple[Dict[int, str], Optional[str]]:
        if not page_numbers:
            return {}, None

        strategy = (self.config.ocr.strategy or "tesseract").lower()
        zero_based = [p - 1 for p in page_numbers if p > 0]
        if not zero_based:
            return {}, None

        texts: Dict[int, str] = {}
        if strategy == "tesseract":
            from pdf_analysis.ingest.ocr import ocr_pages_if_needed

            raw = ocr_pages_if_needed(
                pdf_path, zero_based, lang=self.config.ocr.languages
            )
            texts = {idx + 1: txt.strip() for idx, txt in raw.items() if txt}
        elif strategy == "textract":
            client = self.config.ocr.textract_client
            if client is None:
                self._handle_missing_client(
                    "textract", "Provide boto3 Textract client callable"
                )
            else:
                raw = self._invoke_callable_client(client, pdf_path, zero_based)
                texts = {idx + 1: txt.strip() for idx, txt in raw.items() if txt}
        elif strategy in {"gcv", "vision"}:
            client = self.config.ocr.gcv_client
            if client is None:
                self._handle_missing_client(
                    "google-vision", "Provide Vision API callable"
                )
            else:
                raw = self._invoke_callable_client(client, pdf_path, zero_based)
                texts = {idx + 1: txt.strip() for idx, txt in raw.items() if txt}
        else:
            logger.warning("Unknown OCR strategy %s", strategy)
            return {}, None

        if texts:
            logger.debug(
                "[pipeline] OCR recovered text for pages=%s", sorted(texts.keys())
            )
            return texts, strategy
        return {}, None

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

    def _extract_tables_for_pages(
        self, pdf_path: Path, page_numbers: Sequence[int]
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        tables: List[Dict[str, Any]] = []
        engines_used: List[str] = []
        if not page_numbers:
            return tables, engines_used

        for engine in self.config.structured.table_engines:
            try:
                items = extract_tables(
                    pdf_path,
                    engine=engine,
                    pages=page_numbers,
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
            from langchain_core.documents import Document
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_text_splitters import RecursiveCharacterTextSplitter
        except ModuleNotFoundError as exc:
            self._handle_missing_dependency("langchain", exc)
            return None

        documents = [
            Document(
                page_content=ctx.full_text(), metadata={"source": ctx.pdf_path.name}
            )
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
    def _patch_pdfminer_for_pdfplumber(self) -> None:
        try:
            from pdfminer import pdfinterp as _pdfinterp  # type: ignore

            if not hasattr(_pdfinterp, "PDFStackT"):
                from typing import Any as _Any

                _pdfinterp.PDFStackT = _Any  # type: ignore[attr-defined]
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _compute_metrics(
        self, ctx: PipelineContext, quality: Optional[Dict[str, Any]]
    ) -> PipelineMetrics:
        total_pages = len(ctx.pages)
        pages_with_text = sum(1 for page in ctx.pages if (page.get("text") or "").strip())
        text_coverage = (pages_with_text / total_pages) if total_pages else 0.0

        tables_total = len(ctx.tables)
        table_pages = len(
            {tbl.get("page_number") for tbl in ctx.tables if tbl.get("page_number")}
        )
        table_coverage = (table_pages / total_pages) if total_pages else 0.0

        key_value_pairs = len(ctx.key_values)
        key_score = 1.0 if key_value_pairs > 0 else 0.0

        confidence = 0.6 * text_coverage + 0.3 * min(table_coverage, 1.0) + 0.1 * key_score
        confidence = round(confidence, 3)

        metrics = PipelineMetrics(
            total_pages=total_pages,
            pages_with_text=pages_with_text,
            text_coverage=round(text_coverage, 3),
            ocr_pages=len(ctx.ocr_pages),
            tables_total=tables_total,
            table_pages=table_pages,
            key_value_pairs=key_value_pairs,
            confidence=confidence,
        )

        if quality and isinstance(quality.get("json"), dict):
            quality["json"].setdefault("pipeline_metrics", {})
            quality["json"]["pipeline_metrics"].update(
                {
                    "text_coverage": metrics.text_coverage,
                    "tables_total": metrics.tables_total,
                    "confidence": metrics.confidence,
                }
            )

        return metrics

    # ------------------------------------------------------------------
    def _handle_missing_dependency(self, name: str, exc: Exception) -> None:
        message = f"Optional dependency for {name} not available: {exc}"
        if self.config.raise_on_missing_dependencies:
            raise ModuleNotFoundError(message) from exc
        logger.warning(message)

    def _persist_chunk_to_redis(
        self,
        redis_client: Any,
        redis_key: str,
        chunk: PipelineChunk,
        serializer: Optional[Callable[[PipelineChunk], Any]],
    ) -> None:
        payload = serializer(chunk) if serializer else json.dumps(chunk.to_serializable())
        field = f"chunk:{chunk.chunk_index:05d}"
        redis_client.hset(redis_key, field, payload)

    def _get_redis_client(self, config: RedisStreamingConfig) -> Any | None:
        if self._redis_client_cache is not None:
            return self._redis_client_cache

        if config.client_factory is not None:
            try:
                client = config.client_factory(config)
                self._redis_client_cache = client
                return client
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("[pipeline] redis client factory failed: %s", exc)
                return None

        redis_module = None
        try:
            redis_module = import_module("redis")
        except ModuleNotFoundError as exc:
            self._handle_missing_dependency("redis", exc)
            return None

        if config.url:
            client = redis_module.Redis.from_url(config.url)
        else:
            client = redis_module.Redis(host=config.host, port=config.port, db=config.db)
        self._redis_client_cache = client
        return client

    @staticmethod
    def _close_iterator(iterator: Iterator[Any]) -> None:
        close = getattr(iterator, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
