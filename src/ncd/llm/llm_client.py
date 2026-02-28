# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

from pydantic import BaseModel

from ncd.config.config import settings


class LLMClient:
    def __init__(self, model_name: str | None = None):
        primary_model = model_name or settings.llm_model_name
        fallback_models = _parse_model_list(os.getenv("LLM_FALLBACK_MODELS", ""))
        self.model_candidates = _dedupe_models([primary_model, *fallback_models])
        self.model_name = self.model_candidates[0]
        self._client = None
        self._response_format_supported_by_model: Dict[str, bool] = {}
        self._temperature_supported_by_model: Dict[str, bool] = {}
        self._disable_response_format = _env_flag("LLM_DISABLE_RESPONSE_FORMAT")
        self._disable_explicit_temperature = _env_flag("LLM_DISABLE_TEMPERATURE")

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
        ordered_candidates = [self.model_name] + [
            candidate
            for candidate in self.model_candidates
            if candidate.lower() != self.model_name.lower()
        ]
        last_exc: Exception | None = None
        for idx, candidate in enumerate(ordered_candidates):
            truncated_prompt = user_prompt
            truncation_attempt = 0
            while True:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": truncated_prompt},
                ]
                try:
                    response = self._create_completion(
                        client=client,
                        model_name=candidate,
                        messages=messages,
                        response_format=response_format,
                        temperature=temperature,
                    )
                    # Keep successful candidate sticky for subsequent calls.
                    self.model_name = candidate
                    self.model_candidates = [candidate] + [
                        model
                        for model in self.model_candidates
                        if model.lower() != candidate.lower()
                    ]
                    content = response.choices[0].message.content
                    if not content:
                        raise RuntimeError("OpenAI response missing content")
                    return content
                except Exception as exc:
                    last_exc = exc
                    if _is_context_length_error(exc):
                        next_prompt = _truncate_user_prompt(
                            user_prompt, truncation_attempt
                        )
                        if next_prompt is not None:
                            truncation_attempt += 1
                            truncated_prompt = next_prompt
                            print(
                                f"[WARN] OpenAI context length exceeded for '{candidate}'; "
                                f"retrying with truncated prompt ({truncation_attempt}).",
                                file=sys.stderr,
                            )
                            continue
                    if (
                        (_is_rate_limit_error(exc) or _is_model_unavailable_error(exc))
                        and idx < len(ordered_candidates) - 1
                    ):
                        break
                    raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("OpenAI call failed without an exception")

    def _create_completion(
        self,
        *,
        client: Any,
        model_name: str,
        messages: List[Dict[str, str]],
        response_format: dict | None,
        temperature: float,
    ) -> Any:
        response_format_supported = self._response_format_supported(
            model_name, response_format
        )
        temperature_supported = self._temperature_supported(model_name)

        def _call(*, include_response_format: bool, include_temperature: bool) -> Any:
            kwargs: Dict[str, Any] = {
                "model": model_name,
                "messages": messages,
            }
            if include_temperature:
                kwargs["temperature"] = temperature
            if include_response_format and response_format is not None:
                kwargs["response_format"] = response_format
            return client.chat.completions.create(**kwargs)

        try:
            return _call(
                include_response_format=response_format_supported,
                include_temperature=temperature_supported,
            )
        except Exception as exc:
            # Some models only accept the default temperature and reject explicit values.
            if temperature_supported and self._should_retry_without_temperature(
                exc, temperature
            ):
                self._set_temperature_supported(model_name, False)
                try:
                    return _call(
                        include_response_format=response_format_supported,
                        include_temperature=False,
                    )
                except Exception as no_temp_exc:
                    if response_format_supported and self._should_retry_without_response_format(
                        no_temp_exc, response_format
                    ):
                        self._set_response_format_supported(model_name, False)
                        return _call(
                            include_response_format=False, include_temperature=False
                        )
                    raise

            # Only retry without response_format when the API/model rejects that parameter.
            # Do not swallow transient failures (e.g. 429), so outer retry logic can back off.
            if not response_format_supported or not self._should_retry_without_response_format(
                exc, response_format
            ):
                raise
            self._set_response_format_supported(model_name, False)
            try:
                return _call(
                    include_response_format=False,
                    include_temperature=temperature_supported,
                )
            except Exception as no_format_exc:
                if temperature_supported and self._should_retry_without_temperature(
                    no_format_exc, temperature
                ):
                    self._set_temperature_supported(model_name, False)
                    return _call(
                        include_response_format=False, include_temperature=False
                    )
                raise

    def _response_format_supported(
        self, model_name: str, response_format: dict | None
    ) -> bool:
        if response_format is None or self._disable_response_format:
            return False
        return self._response_format_supported_by_model.get(model_name.lower(), True)

    def _set_response_format_supported(self, model_name: str, supported: bool) -> None:
        self._response_format_supported_by_model[model_name.lower()] = supported

    def _temperature_supported(self, model_name: str) -> bool:
        if self._disable_explicit_temperature:
            return False
        return self._temperature_supported_by_model.get(model_name.lower(), True)

    def _set_temperature_supported(self, model_name: str, supported: bool) -> None:
        self._temperature_supported_by_model[model_name.lower()] = supported

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

    @staticmethod
    def _should_retry_without_response_format(
        exc: Exception, response_format: dict | None
    ) -> bool:
        if response_format is None:
            return False
        message = str(exc).lower()
        signal_phrases = (
            "response_format",
            "json_object",
            "unsupported",
            "not supported",
            "unknown parameter",
            "invalid parameter",
        )
        return any(phrase in message for phrase in signal_phrases)

    @staticmethod
    def _should_retry_without_temperature(exc: Exception, temperature: float) -> bool:
        # Preserve current behavior for default temperature.
        if temperature == 1:
            return False
        message = str(exc).lower()
        if "temperature" not in message:
            return False
        signal_phrases = (
            "unsupported value",
            "does not support",
            "only the default",
            "invalid parameter",
            "unknown parameter",
        )
        return any(phrase in message for phrase in signal_phrases)


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


def _parse_model_list(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _dedupe_models(models: List[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for model in models:
        key = model.strip()
        if not key:
            continue
        lowered = key.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(key)
    return ordered


def _is_rate_limit_error(exc: Exception) -> bool:
    message = str(exc).lower()
    if "too many requests" in message or "rate limit" in message or "429" in message:
        return True
    try:
        from openai import RateLimitError

        return isinstance(exc, RateLimitError)
    except Exception:
        return False


def _is_model_unavailable_error(exc: Exception) -> bool:
    message = str(exc).lower()
    signals = (
        "model_not_found",
        "does not exist or you do not have access",
        "you do not have access to this model",
        "unknown model",
        "invalid model",
    )
    return any(signal in message for signal in signals)


def _is_context_length_error(exc: Exception) -> bool:
    message = str(exc).lower()
    signals = (
        "context_length_exceeded",
        "maximum context length",
        "context window",
        "too many tokens",
        "please reduce your prompt",
        "prompt is too long",
    )
    return any(signal in message for signal in signals)


def _truncate_user_prompt(user_prompt: str, attempt: int) -> str | None:
    ratios = (0.8, 0.6, 0.45)
    if attempt >= len(ratios):
        return None
    original = str(user_prompt or "")
    if not original:
        return None

    target_len = max(600, int(len(original) * ratios[attempt]))
    if target_len >= len(original):
        return None

    omitted_chars = len(original) - target_len
    suffix = (
        "\n\n[TRUNCATED FOR CONTEXT LIMIT: "
        f"{omitted_chars} trailing characters omitted.]"
    )
    keep_len = max(0, target_len - len(suffix))
    return f"{original[:keep_len].rstrip()}{suffix}"


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}
