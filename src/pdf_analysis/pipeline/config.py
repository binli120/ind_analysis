"""Configuration objects for the multi-stage PDF processing pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple


@dataclass(slots=True)
class TextExtractionConfig:
    """Controls sequential pure-text extraction strategies."""

    engines: Sequence[str] = ("pdfplumber", "pymupdf", "pdfminer")
    max_pages: Optional[int] = None
    keep_intermediate: bool = False
    detect_layout: bool = True


@dataclass(slots=True)
class OCRConfig:
    """Controls OCR fallbacks when native text extraction fails."""

    enable: bool = True
    strategy: str = "tesseract"
    languages: str = "eng"
    textract_client: Any = None
    gcv_client: Any = None
    parallelism: int = 2


@dataclass(slots=True)
class StructuredExtractionConfig:
    """Configures table/form/key-value extraction."""

    table_engines: Sequence[str] = ("pdfplumber", "camelot", "tabula")
    key_value_patterns: Dict[str, str] = field(default_factory=dict)
    enable_form_recognizer: bool = False
    enable_document_ai: bool = False
    form_recognizer_client: Any = None
    document_ai_client: Any = None


@dataclass(slots=True)
class LangChainConfig:
    """Configures optional LangChain-powered LLM post-processing."""

    enabled: bool = False
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    temperature: float = 0.0
    prompt_template: str = (
        "You are a diligent analyst. Given the extracted content, "
        "produce a structured JSON summary with high-confidence fields."
    )
    chunk_size: int = 2000
    chunk_overlap: int = 200
    output_format: str = "json"
    factory: Any = None


@dataclass(slots=True)
class PipelineConfig:
    """Aggregates all stage configurations."""

    text: TextExtractionConfig = field(default_factory=TextExtractionConfig)
    ocr: OCRConfig = field(default_factory=OCRConfig)
    structured: StructuredExtractionConfig = field(default_factory=StructuredExtractionConfig)
    llm: LangChainConfig = field(default_factory=LangChainConfig)
    raise_on_missing_dependencies: bool = False


DEFAULT_PIPELINE_CONFIG = PipelineConfig()
