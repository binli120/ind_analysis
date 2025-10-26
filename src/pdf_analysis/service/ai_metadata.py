# author: Bin Lee
# email: blee@filynai.com

from __future__ import annotations

import json
import logging
import os
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

    def __init__(self, model: str = "gpt-4o-mini", max_chars: int = 6000) -> None:
        self.model = model
        self.max_chars = max_chars
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
            "You are an assistant that labels regulatory PDF documents. "
            "Respond ONLY with minified JSON using the schema: "
            '{"labels": ["..."], "keywords": ["..."], "language": "en"}. '
            "Provide at most 3 descriptive labels (lowercase) and up to 5 concise keywords. "
            "Infer the primary language as a two-letter ISO code. "
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

        if labels:
            metadata["labels"] = labels
        if keywords:
            metadata["keywords"] = keywords
        metadata["language"] = language[:10] if language else "unknown"
        metadata["analyzed"] = True
        return metadata


def _extract_text_from_response(response: Any) -> str:
    """
    Normalises the OpenAI responses API result into a text blob.
    """
    if hasattr(response, "output") and response.output:
        fragments: List[str] = []
        for item in response.output:
            for piece in item.get("content", []):
                text = piece.get("text")
                if text:
                    fragments.append(text)
        return _strip_code_fence("".join(fragments))

    if hasattr(response, "choices") and response.choices:  # compatibility fallback
        message = response.choices[0].get("message")
        if message and "content" in message:
            return _strip_code_fence(message["content"])
    return ""


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    return stripped.strip()
