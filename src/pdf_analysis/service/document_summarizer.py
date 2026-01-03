# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""LLM-powered document summary and topic generation."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from .ai_metadata import _extract_text_from_response

logger = logging.getLogger(__name__)

try:  # Optional dependency (installed via infra extras)
    from openai import OpenAI
except ModuleNotFoundError:  # pragma: no cover - handled gracefully
    OpenAI = None  # type: ignore[misc]


@dataclass(slots=True)
class TopicSection:
    """Represents a navigable topic anchor within a document."""

    title: str
    description: str
    anchor: str


@dataclass(slots=True)
class TopicSummaryResult:
    """Container for a document summary and its key topics."""

    summary: str
    topics: list[TopicSection]


class OpenAIDocumentSummarizer:
    """
    Generates a natural-language summary plus topic anchors for a document body.
    """

    def __init__(
        self, model: str = "gpt-4o-mini", max_chars: int = 12000, max_topics: int = 8
    ) -> None:
        self.model = model
        self.max_chars = max_chars
        self.max_topics = max_topics

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
                "OpenAI summary generation disabled. Install the 'infra' extras and set OPENAI_API_KEY."
            )
            self._client: Optional[OpenAI] = None
        else:
            self._client = OpenAI(api_key=api_key)

    def is_available(self) -> bool:
        return self._client is not None

    def __call__(self, markdown_body: str) -> Optional[TopicSummaryResult]:
        if not self._client or not markdown_body.strip():
            return None

        truncated = markdown_body[: self.max_chars]
        system_prompt = (
            "You analyse regulatory documents and craft concise executive summaries. "
            "Produce a JSON object with two keys: "
            '"summary" (string, <= 8 sentences) and "topics" (array of up to '
            f"{self.max_topics} objects). Each topic object must contain "
            '{"title": "...", "description": "2-3 sentence overview"}. '
            "Do not include markdown links in the JSON. Focus on distinct major sections."
        )

        try:
            response = self._client.responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": truncated},
                ],
                temperature=0.4,
            )
        except Exception as exc:  # pragma: no cover - network or API errors
            logger.warning("OpenAI summary generation failed: %s", exc)
            return None

        raw_text = _extract_text_from_response(response)
        if not raw_text:
            return None

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning("OpenAI summary response was not valid JSON: %s", raw_text)
            return None

        summary = str(data.get("summary") or "").strip()
        topics_payload = data.get("topics") or []
        topics: List[TopicSection] = []
        seen: set[str] = set()
        for entry in topics_payload:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or entry.get("name") or "").strip()
            description = str(
                entry.get("description") or entry.get("summary") or ""
            ).strip()
            if not title:
                continue
            anchor = _slugify(title)
            if not anchor:
                continue
            candidate = anchor
            counter = 2
            while candidate in seen:
                candidate = f"{anchor}-{counter}"
                counter += 1
            seen.add(candidate)
            topics.append(
                TopicSection(title=title, description=description, anchor=candidate)
            )
            if len(topics) >= self.max_topics:
                break

        if not summary and not topics:
            return None
        return TopicSummaryResult(summary=summary, topics=topics)


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
    return cleaned.strip("-")


def format_summary_text(summary: TopicSummaryResult) -> str:
    lines: List[str] = []
    if summary.summary:
        lines.append("Document Summary")
        lines.append("=================")
        lines.append(summary.summary.strip())
    if summary.topics:
        if lines:
            lines.append("")
        lines.append("Key Topics")
        lines.append("----------")
        for topic in summary.topics:
            desc = f" — {topic.description.strip()}" if topic.description else ""
            lines.append(f"- [{topic.title}](#{topic.anchor}){desc}")
    return "\n".join(lines).strip()


def embed_topics_into_markdown(markdown: str, topics: List[TopicSection]) -> str:
    if not topics:
        return markdown
    topic_section_lines = ["## Key Topics"]
    for topic in topics:
        desc = f" — {topic.description.strip()}" if topic.description else ""
        topic_section_lines.append(f"- [{topic.title}](#{topic.anchor}){desc}")
    topic_section = "\n".join(topic_section_lines) + "\n\n"
    updated = topic_section + markdown

    for topic in topics:
        anchor_tag = f'<a id="{topic.anchor}"></a>'
        if anchor_tag in updated:
            continue
        pattern = re.compile(re.escape(topic.title), re.IGNORECASE)
        match = pattern.search(updated)
        if match:
            start = match.start()
            updated = f"{updated[:start]}{anchor_tag}{updated[start:]}"
        else:
            updated = f"{updated}\n\n{anchor_tag}\n"
    return updated
