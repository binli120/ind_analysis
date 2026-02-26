"""Shared metadata key constants used across PDF analysis and IND labeling modules."""

from __future__ import annotations

LABELS_KEY = "labels"
KEYWORDS_KEY = "keywords"
LANGUAGE_KEY = "language"
ANALYZED_KEY = "analyzed"

IND_DOCUMENT_TYPE_KEY = "ind_document_type"
IND_SECTION_NUMBER_KEY = "ind_section_number"
IND_SECTION_TITLE_KEY = "ind_section_title"
IND_CLASSIFICATION_CONFIDENCE_KEY = "ind_classification_confidence"

CLASSIFICATION_METHOD_KEY = "classification_method"
SOURCE_KEY_KEY = "source_key"
PAGES_SAMPLED_KEY = "pages_sampled"

SUMMARY_KEY = "summary"
QUALITY_MARKDOWN_KEY = "quality_markdown"

LLM_LABEL_KEY = "label"
LLM_TAGS_KEY = "tags"
LLM_LANG_KEY = "lang"
LLM_DOCUMENT_TYPE_KEY = "document_type"
LLM_SECTION_NUMBER_KEY = "section_number"
LLM_SECTION_TITLE_KEY = "section_title"
LLM_CONFIDENCE_KEY = "ind_confidence"
LLM_CONFIDENCE_FALLBACK_KEY = "confidence"

IND_CLASSIFICATION_METADATA_KEYS = (
    IND_DOCUMENT_TYPE_KEY,
    IND_SECTION_NUMBER_KEY,
    IND_SECTION_TITLE_KEY,
    IND_CLASSIFICATION_CONFIDENCE_KEY,
)

