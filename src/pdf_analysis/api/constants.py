# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

from __future__ import annotations

import os
import re

APP_TITLE = "PDF Analysis API"
APP_DESCRIPTION = (
    "Upload a PDF study report and receive extracted content, structured tables, "
    "and quality analysis."
)
APP_VERSION = "0.1.0"

CORS_ALLOW_ORIGINS = ["https://ind-manager-v2.vercel.app"]
CORS_ALLOW_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_METHODS = ["*"]
CORS_ALLOW_HEADERS = ["*"]

UPLOAD_ROUTER_TAGS = ["upload"]
NCD_ROUTER_TAGS = ["ncd"]
DEV_ROUTER_TAGS = ["dev"]
NCD_ROUTER_PREFIX = "/ncd"
DEV_ROUTER_PREFIX = "/dev"

DEFAULT_TEMPLATE_BUCKET = os.getenv("IND_TEMPLATES_BUCKET", "indtemplates")
DEFAULT_TEMPLATE_PREFIXES = ("2.4/", "2.6/")

SECTION_PROMPT_MAX_CHARS = 24000
USER_PROMPT_MAX_CHARS = int(os.getenv("USER_PROMPT_MAX_CHARS", "4000"))
LLM_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("LLM_RATE_LIMIT_WINDOW_SECONDS", "60"))
LLM_RATE_LIMIT_SUMMARY_REQUESTS = int(os.getenv("LLM_RATE_LIMIT_SUMMARY_REQUESTS", "6"))
LLM_RATE_LIMIT_TABULATED_REQUESTS = int(
    os.getenv("LLM_RATE_LIMIT_TABULATED_REQUESTS", "6")
)
LLM_RATE_LIMIT_APPROVE_REQUESTS = int(os.getenv("LLM_RATE_LIMIT_APPROVE_REQUESTS", "12"))

STUDY_ID_RE = re.compile(
    r"\b(?=[A-Z0-9./-]*[A-Z])(?=[A-Z0-9./-]*\d)[A-Z0-9]{2,}(?:[-./][A-Z0-9]+)+\b",
    re.IGNORECASE,
)
STUDY_ID_EXT_RE = re.compile(r"\.(?:pdf|xml|docx|txt|csv|json|md)$", re.IGNORECASE)
STUDY_ID_PREFIX_RE = re.compile(r"^(?:stf-|study-)", re.IGNORECASE)
STUDY_ID_TRAILERS = {
    "pdf",
    "xml",
    "docx",
    "txt",
    "csv",
    "json",
    "md",
    "extracted",
    "quality",
    "meta",
    "images",
    "tables",
}
STUDY_ID_SKIP_RE = re.compile(
    r"^(?:input\.(?:p\d+)?\.t\d+|p\d+\.t\d+)$", re.IGNORECASE
)
