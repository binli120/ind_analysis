# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

from __future__ import annotations

import json
import os
from typing import Any, Dict

from pydantic import BaseModel

from .config import settings


class LLMClient:
    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or settings.llm_model_name
        self._client = None

    def extract_json(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: BaseModel | None = None,
    ) -> Dict[str, Any]:
        """
        Call OpenAI and parse JSON response.
        """
        content = self._chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        data = _parse_json_payload(content)
        if response_model is not None:
            return response_model(**data).model_dump()
        return data

    def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        """
        Call OpenAI for free-form generation (2.4 / 2.6 text).
        """
        return self._chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_format=None,
            temperature=0.2,
        )

    def _chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_format: dict | None,
        temperature: float,
    ) -> str:
        client = self._get_client()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        try:
            response = client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                temperature=temperature,
                response_format=response_format,
            )
        except Exception:
            response = client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                temperature=temperature,
            )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("OpenAI response missing content")
        return content

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "openai is required for LLMClient; install with `poetry add openai`."
            ) from exc
        api_key = os.getenv("OPENAI_API_KEY") or settings.llm_api_key
        if not api_key or api_key == "YOUR_API_KEY":
            raise RuntimeError("OPENAI_API_KEY is required for LLMClient")
        self._client = OpenAI(api_key=api_key)
        return self._client


def _parse_json_payload(content: str) -> Dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    start = min(
        (idx for idx in (content.find("{"), content.find("[")) if idx >= 0),
        default=-1,
    )
    if start == -1:
        raise ValueError("No JSON object found in LLM response")

    candidate = content[start:].strip()
    end = max(candidate.rfind("}"), candidate.rfind("]"))
    if end == -1:
        raise ValueError("No JSON object found in LLM response")
    candidate = candidate[: end + 1]
    return json.loads(candidate)
