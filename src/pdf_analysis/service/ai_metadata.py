# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""LLM-powered metadata generation utilities for IND document classification."""

# author: Bin Lee
# email: blee@filynai.com

from __future__ import annotations

import json
import logging
import os
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:  # Optional dependency loaded when infra extras are installed.
    from openai import OpenAI
except ModuleNotFoundError:  # pragma: no cover - handled gracefully at runtime
    OpenAI = None  # type: ignore[misc]


def _ensure_list(values: Any, limit: int) -> List[str]:
    if isinstance(values, str):
        items = [segment.strip() for segment in values.split(",") if segment.strip()]
    elif isinstance(values, (list, tuple, set)):
        items = [str(item).strip() for item in values if str(item).strip()]
    else:
        items = []
    deduplicated: List[str] = []
    for item in items:
        lowered = item.lower()
        if lowered and lowered not in {entry.lower() for entry in deduplicated}:
            deduplicated.append(item)
        if len(deduplicated) >= limit:
            break
    return deduplicated[:limit]


class OpenAIMetadataGenerator:
    """
    Generates lightweight document metadata (labels, keywords, language)
    using the OpenAI Responses API.
    """

    _SECTION_GUIDE = textwrap.dedent(
        """
        Common IND (Investigational New Drug) sections you may map to using zero-shot reasoning:
        - 1: Administrative information (forms, sponsor statements, cover letters, meeting minutes)
        - 1.1: Forms (e.g., FDA Form 1571, Form 1572, financial disclosure forms)
        - 1.2: Cover letters and general correspondence
        - 1.3: Sponsor / applicant information (contact details, letters of authorization)
        - 1.4: Financial disclosure information
        - 1.5: Application tracking (amendment history, serial numbers)
        - 2: Summaries (overall development program, clinical/nonclinical summaries)
        - 2.3: Quality overall summary (CMC overview)
        - 2.4: Nonclinical overview
        - 2.5: Clinical overview
        - 3: Quality (CMC) documentation (drug substance, drug product, manufacturing, controls)
        - 4: Nonclinical study reports (pharmacology, pharmacokinetics, toxicology)
        - 5: Clinical study reports and related information (protocols, CSR, IB, amendments)
        - 5.3: Clinical study reports (Phase 1/2/3, bioavailability/bioequivalence)
        - 5.4: Literature references and additional clinical information
        - Safety reports: IND safety updates, SUSARs, annual safety reports
        When uncertain, choose the closest match or return null values with reduced confidence.
        """
    ).strip()

    def __init__(self, model: str = "gpt-4o-mini", max_chars: int = 6000) -> None:
        self.model = model
        self.max_chars = max_chars
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            try:
                from dotenv import load_dotenv  # type: ignore
            except ModuleNotFoundError:
                load_dotenv = None  # type: ignore[assignment]
            if load_dotenv:
                for candidate in (".env.local", ".env"):
                    env_path = Path(candidate)
                    if env_path.exists():
                        load_dotenv(dotenv_path=env_path, override=False)
                api_key = os.getenv("OPENAI_API_KEY")
        if OpenAI is None or not api_key:
            logger.warning(
                "OpenAI metadata generation disabled. Install the 'infra' extras and set OPENAI_API_KEY."
            )
            self._client: Optional[OpenAI] = None
        else:
            self._client = OpenAI(api_key=api_key)

    def __call__(self, text: str) -> Dict[str, Any]:
        if not self._client:
            return {}

        truncated = text[: self.max_chars]
        system_prompt = (
            "You are an assistant that labels regulatory IND PDF documents. "
            "Use zero-shot reasoning to infer the most likely IND document type and section. "
            "Leverage the following guide:\n"
            f"{self._SECTION_GUIDE}\n"
            "Respond ONLY with minified JSON using the schema: "
            + '{"labels": ["..."], "keywords": ["..."], "language": "en", '
            '"ind_document_type": "...", "ind_section_number": "...", '
            '"ind_section_title": "...", "ind_confidence": 0.0}. '
            "Provide at most 3 descriptive labels (lowercase) and up to 5 concise keywords. "
            "Use two-letter ISO codes for language. "
            "When uncertain, set ind_document_type, ind_section_number, or ind_section_title to null "
            "and reduce ind_confidence (0.0-1.0). "
            "Do not include any explanation or additional text."
        )

        try:
            response = self._client.responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": truncated},
                ],
                temperature=0.1,
            )
        except Exception as exc:  # pragma: no cover - network or API errors
            logger.warning("OpenAI metadata generation failed: %s", exc)
            return {}

        raw_text = _extract_text_from_response(response)
        if not raw_text:
            return {}

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning("OpenAI metadata response was not valid JSON: %s", raw_text)
            return {}

        metadata: Dict[str, Any] = {}
        labels = _ensure_list(data.get("labels") or data.get("label"), limit=3)
        keywords = _ensure_list(data.get("keywords") or data.get("tags"), limit=5)
        language = str(data.get("language") or data.get("lang") or "unknown").lower()
        ind_document_type = data.get("ind_document_type") or data.get("document_type")
        ind_section_number = data.get("ind_section_number") or data.get(
            "section_number"
        )
        ind_section_title = data.get("ind_section_title") or data.get("section_title")
        ind_confidence = data.get("ind_confidence") or data.get("confidence")

        if labels:
            metadata["labels"] = labels
        if keywords:
            metadata["keywords"] = keywords
        metadata["language"] = language[:10] if language else "unknown"
        if ind_document_type:
            metadata["ind_document_type"] = str(ind_document_type).strip()
        if ind_section_number:
            metadata["ind_section_number"] = str(ind_section_number).strip()
        if ind_section_title:
            metadata["ind_section_title"] = str(ind_section_title).strip()
        if ind_confidence is not None:
            try:
                metadata["ind_classification_confidence"] = round(
                    float(ind_confidence), 3
                )
            except (TypeError, ValueError):
                pass
        metadata["analyzed"] = True
        return metadata


def _extract_text_from_response(response: Any) -> str:
    """
    Normalises the OpenAI responses API result into a text blob.
    """
    # Newer OpenAI Python client (Responses API) returns Pydantic models.
    if hasattr(response, "model_dump"):
        try:
            payload = response.model_dump()
        except Exception:  # pragma: no cover - defensive
            payload = {}
        if payload:
            text = _extract_text_from_payload(payload)
            if text:
                return _strip_code_fence(text)

    # Legacy dictionary-like structure.
    if hasattr(response, "output") and response.output:
        fragments: List[str] = []
        for item in response.output:
            content = getattr(item, "content", None)
            if content is None and hasattr(item, "model_dump"):
                content = item.model_dump().get("content")
            if content is None and isinstance(item, dict):
                content = item.get("content")
            if not content:
                continue
            fragments.extend(_collect_text_fragments(content))
        if fragments:
            return _strip_code_fence("".join(fragments))

    if hasattr(response, "choices") and response.choices:  # compatibility fallback
        message = response.choices[0].get("message")
        if message and "content" in message:
            return _strip_code_fence(message["content"])
    return ""


def _collect_text_fragments(content: Any) -> List[str]:
    fragments: List[str] = []
    if not content:
        return fragments
    if isinstance(content, dict):
        fragments.extend(_collect_text_fragments([content]))
        return fragments
    for block in content:
        if block is None:
            continue
        if hasattr(block, "model_dump"):
            block_payload = block.model_dump()
        elif isinstance(block, dict):
            block_payload = block
        else:
            block_payload = {}
        text_payload = block_payload.get("text")
        if text_payload is None and hasattr(block, "text"):
            text_payload = block.text
        if text_payload is None and "content" in block_payload:
            fragments.extend(_collect_text_fragments(block_payload.get("content")))
            continue
        if isinstance(text_payload, dict):
            value = text_payload.get("value") or text_payload.get("text")
        else:
            value = text_payload
        if value:
            fragments.append(str(value))
    return fragments


def _extract_text_from_payload(payload: Dict[str, Any]) -> str:
    output = payload.get("output")
    fragments = _collect_text_fragments(output)
    if not fragments:
        # Some responses expose rich content under response["data"] -> text
        data_entries = payload.get("data")
        fragments.extend(_collect_text_fragments(data_entries))
    return "".join(fragments)


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    return stripped.strip()
