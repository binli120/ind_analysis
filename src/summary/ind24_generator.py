"""End-to-end helper for generating IND Section 2.4 from Section 2.6 inputs."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import time

from pdf_analysis.pipeline import PDFProcessingPipeline, PipelineConfig
from pdf_analysis.service.ai_metadata import _extract_text_from_response

logger = logging.getLogger(__name__)

try:
    from openai import OpenAI
except ModuleNotFoundError:  # pragma: no cover - handled gracefully at runtime
    OpenAI = None  # type: ignore[misc]

try:
    import boto3
except ModuleNotFoundError:  # pragma: no cover - handled gracefully at runtime
    boto3 = None  # type: ignore[misc]

try:
    import redis  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - handled gracefully at runtime
    redis = None  # type: ignore[misc]

_SECTION26_PATTERN = re.compile(r"2\.6(\.[0-9]+)*", re.IGNORECASE)


@dataclass(slots=True)
class Section26Document:
    """Container for Section 2.6 markdown sources."""

    key: str
    markdown: str
    version_id: Optional[str] = None
    last_modified: Optional[datetime] = None
    source: str = "s3"


@dataclass(slots=True)
class _S3ObjectRef:
    key: str
    version_id: Optional[str] = None
    last_modified: Optional[datetime] = None


@dataclass(slots=True)
class IND24GenerationConfig:
    """
    Configuration for orchestrating Section 2.4 generation from S3 + Redis inputs.
    """

    bucket: str
    project: str = "LT1009"
    company: str = "filynai.com"
    section_prefix: Optional[str] = None
    redis_url: Optional[str] = None
    redis_client: Any | None = None
    redis_key_template: str = "{company}:{project_slug}:{module_slug}:{filename}"
    pipeline_config: PipelineConfig = field(default_factory=PipelineConfig)
    output_gap_key: Optional[str] = None
    output_gap_json_key: Optional[str] = None
    output_summary_key: Optional[str] = None
    output_summary_json_key: Optional[str] = None
    output_combined_markdown_key: Optional[str] = None
    gap_model: str = "gpt-4o-mini"
    summary_model_override: Optional[str] = None
    llm_timeout: float = 120.0
    max_chunks: int = 12
    max_gap_tokens_per_chunk: int = 20000
    max_summary_tokens_per_chunk: int = 20000
    max_gap_chars: int = 50000
    max_summary_chars: int = 80000
    max_documents: Optional[int] = None
    write_back_markdown: bool = True
    reference_summary_prefix: Optional[str] = None
    max_reference_summaries: int = 2
    reference_summary_token_limit: int = 8000
    guideline_path: Path = field(
        default_factory=lambda: Path(__file__).with_name("IND_2.4_Generation_Guideline.md")
    )
    template_path: Path = field(
        default_factory=lambda: Path(__file__).with_name("ind_2_4_generation_template.json")
    )


class Section26MarkdownCollector:
    """
    Fetches Section 2.6 markdown from Redis/S3, backfilling via the PDF pipeline when needed.
    """

    def __init__(self, config: IND24GenerationConfig) -> None:
        self.config = config
        self._s3 = self._build_s3_client()
        self._redis = self._build_redis_client()
        self._pipeline = PDFProcessingPipeline(config=config.pipeline_config)

    # ------------------------------------------------------------------
    def collect(self) -> Tuple[List[Section26Document], str, str]:
        documents: List[Section26Document] = []
        combined_parts: List[str] = []
        output_prefix: Optional[str] = None

        for obj in self._iter_section_objects():
            if self.config.max_documents and len(documents) >= self.config.max_documents:
                break

            suffix = obj.key.lower()
            markdown_body: Optional[str] = None
            source = "s3"

            if suffix.endswith(".md"):
                markdown_body = self._download_text(obj.key, obj.version_id)
            elif suffix.endswith(".pdf"):
                markdown_body, source = self._resolve_pdf_markdown(obj)
            else:
                continue

            if not markdown_body:
                logger.debug("No markdown resolved for %s; skipping", obj.key)
                continue

            documents.append(
                Section26Document(
                    key=obj.key,
                    markdown=markdown_body,
                    version_id=obj.version_id,
                    last_modified=obj.last_modified,
                    source=source,
                )
            )
            combined_parts.append(f"# Source: {obj.key}\n\n{markdown_body.strip()}\n")
            if output_prefix is None:
                output_prefix = self._infer_output_prefix(obj.key)

        combined_markdown = "\n".join(combined_parts).strip()
        derived_prefix = output_prefix or self._default_output_prefix()
        return documents, combined_markdown, derived_prefix

    # ------------------------------------------------------------------
    def _iter_section_objects(self) -> Iterable[_S3ObjectRef]:
        base_prefix = (self.config.section_prefix or "").rstrip("/")
        if not base_prefix:
            base_prefix = f"{self.config.company.rstrip('/')}/{self.config.project}/"
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.config.bucket, Prefix=base_prefix):
            for entry in page.get("Contents", []):
                key = entry.get("Key")
                if not key or not self._is_section26_key(key):
                    continue
                lowered = key.lower()
                if lowered.endswith(".meta.json") or lowered.endswith(".summary.txt"):
                    continue
                if not (lowered.endswith(".md") or lowered.endswith(".pdf")):
                    continue
                last_modified = entry.get("LastModified")
                yield _S3ObjectRef(
                    key=key,
                    version_id=None,
                    last_modified=last_modified if isinstance(last_modified, datetime) else None,
                )

    def _is_section26_key(self, key: str) -> bool:
        parts = key.split("/")
        if len(parts) < 3:
            return False
        if parts[0] != self.config.company or parts[1] != self.config.project:
            return False
        if self.config.section_prefix:
            if not key.startswith(self.config.section_prefix.rstrip("/") + "/"):
                return False
        return any(_SECTION26_PATTERN.search(part) for part in parts)

    # ------------------------------------------------------------------
    def _resolve_pdf_markdown(self, obj: _S3ObjectRef) -> Tuple[Optional[str], str]:
        redis_body = self._fetch_from_redis(obj.key)
        if redis_body:
            return redis_body, "redis"

        s3_body = self._download_text(f"{obj.key}.md", obj.version_id)
        if s3_body:
            return s3_body, "s3"

        generated = self._generate_markdown(obj.key, obj.version_id)
        if generated and self.config.write_back_markdown:
            self._upload_markdown(obj.key, generated)
        return generated, "pipeline"

    def _fetch_from_redis(self, s3_key: str) -> Optional[str]:
        if not self._redis:
            return None
        redis_key = self._derive_redis_key(s3_key)
        if not redis_key:
            return None
        try:
            value = self._redis.hget(redis_key, "markdown")
        except Exception:  # pragma: no cover - external service
            logger.debug("Redis lookup failed for %s", redis_key, exc_info=True)
            return None
        if value is None:
            return None
        return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)

    def _derive_redis_key(self, s3_key: str) -> Optional[str]:
        parts = s3_key.split("/")
        if len(parts) < 3:
            return None
        company, project, module_label = parts[0], parts[1], parts[2]
        project_slug = _slugify(project)
        module_slug = _slugify(module_label)
        filename = parts[-1]
        try:
            return self.config.redis_key_template.format(
                company=company,
                project=project,
                project_slug=project_slug,
                module=module_label,
                module_slug=module_slug,
                filename=filename,
            )
        except Exception:  # pragma: no cover - defensive formatting
            logger.debug("Unable to format redis key for %s", s3_key, exc_info=True)
            return None

    # ------------------------------------------------------------------
    def _download_text(self, key: str, version_id: Optional[str]) -> Optional[str]:
        try:
            extra: Dict[str, Any] = {"VersionId": version_id} if version_id else {}
            response = self._s3.get_object(Bucket=self.config.bucket, Key=key, **extra)
            body = response.get("Body")
            if body is None:
                return None
            raw = body.read()
            return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except Exception:  # pragma: no cover - external service
            return None

    def _generate_markdown(self, key: str, version_id: Optional[str]) -> Optional[str]:
        with tempfile.NamedTemporaryFile(suffix=Path(key).suffix or ".pdf", delete=False) as tmp:
            try:
                extra: Dict[str, Any] = {"VersionId": version_id} if version_id else {}
                self._s3.download_fileobj(self.config.bucket, key, tmp, ExtraArgs=extra)
                tmp.flush()
                result = self._pipeline.run(Path(tmp.name))
                return result.markdown
            except Exception as exc:  # pragma: no cover - external dependencies
                logger.warning("Failed to generate markdown for %s: %s", key, exc)
                return None
            finally:
                try:
                    Path(tmp.name).unlink(missing_ok=True)
                except Exception:
                    logger.debug("Unable to remove temp file %s", tmp.name, exc_info=True)

    def _upload_markdown(self, pdf_key: str, markdown: str) -> None:
        md_key = f"{pdf_key}.md"
        try:
            self._s3.put_object(
                Bucket=self.config.bucket,
                Key=md_key,
                Body=markdown.encode("utf-8"),
                ContentType="text/markdown",
            )
            logger.info("Uploaded generated markdown to %s", md_key)
        except Exception:  # pragma: no cover - external service
            logger.warning("Unable to upload markdown to %s", md_key, exc_info=True)

    # ------------------------------------------------------------------
    def _infer_output_prefix(self, key: str) -> str:
        parts = key.split("/")
        for idx, segment in enumerate(parts):
            if _SECTION26_PATTERN.search(segment):
                return "/".join(parts[: idx + 1])
        return "/".join(parts[:-1])

    def _default_output_prefix(self) -> str:
        if self.config.section_prefix:
            return self.config.section_prefix.rstrip("/")
        return f"{self.config.company}/{self.config.project}"

    # ------------------------------------------------------------------
    def fetch_reference_summaries(self) -> List[Dict[str, Any]]:
        """Locate existing 2.4 summaries for few-shot conditioning."""
        if not self._s3:
            return []
        base_prefix = self.config.reference_summary_prefix or f"{self.config.company.rstrip('/')}/{self.config.project}/"
        candidates: List[_S3ObjectRef] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.config.bucket, Prefix=base_prefix):
            for entry in page.get("Contents", []):
                key = entry.get("Key")
                if not key:
                    continue
                lower = key.lower()
                if "2.4" not in lower or "summary" not in lower:
                    continue
                if not (lower.endswith(".md") or lower.endswith(".json")):
                    continue
                lm = entry.get("LastModified")
                candidates.append(
                    _S3ObjectRef(
                        key=key,
                        version_id=None,
                        last_modified=lm if isinstance(lm, datetime) else None,
                    )
                )
        candidates.sort(key=lambda x: x.last_modified or datetime.now(), reverse=True)
        selected = candidates[: max(self.config.max_reference_summaries, 0)]
        results: List[Dict[str, Any]] = []
        for ref in selected:
            text = self._download_text(ref.key, ref.version_id)
            if not text:
                continue
            token_est = _estimate_tokens(text)
            if token_est > self.config.reference_summary_token_limit:
                text = text[: self.config.reference_summary_token_limit * 4]
            results.append({"key": ref.key, "text": text})
        return results

    # ------------------------------------------------------------------
    def _build_s3_client(self) -> Any:
        if boto3 is None:
            raise RuntimeError("boto3 is required for Section 2.6 collection.")
        return boto3.client("s3")

    def _build_redis_client(self) -> Any | None:
        if self.config.redis_client is not None:
            return self.config.redis_client
        if not self.config.redis_url:
            return None
        if redis is None:
            logger.warning("redis-py is not installed; redis cache will be skipped.")
            return None
        return redis.Redis.from_url(self.config.redis_url)


class IND24LLMClient:
    """Handles OpenAI calls for gap analysis and summary generation."""

    def __init__(self, config: IND24GenerationConfig) -> None:
        self.config = config
        self._client = self._build_client()
        self.guideline_text = self._load_guideline(config.guideline_path)
        self.template_payload = self._load_template(config.template_path)

    def _build_client(self) -> Any | None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            for candidate in (".env.local", ".env"):
                env_path = Path(candidate)
                if env_path.exists():
                    try:
                        from dotenv import load_dotenv  # type: ignore
                    except ModuleNotFoundError:
                        load_dotenv = None  # type: ignore[assignment]
                    if load_dotenv:
                        load_dotenv(env_path, override=False)
                        api_key = os.getenv("OPENAI_API_KEY")
                        break
        if not api_key or OpenAI is None:
            logger.warning("OpenAI client unavailable; set OPENAI_API_KEY and install infra extras.")
            return None
        return OpenAI(api_key=api_key)

    # ------------------------------------------------------------------
    def run_gap_analysis(self, combined_markdown: str) -> Dict[str, Any]:
        if not self._client:
            raise RuntimeError("OpenAI client is not available for gap analysis.")
        truncated = _truncate_text(
            combined_markdown, self.config.max_gap_chars, label="Section 2.6 content"
        )
        prompt = (
            "You are a nonclinical regulatory gap analyst. "
            "Using the IND 2.4 generation guideline, identify missing or weak elements in Section 2.6. "
            "Return compact JSON with keys: missing_sections (array of {section_number, section_name, "
            "severity: CRITICAL|MAJOR|MINOR, description, recommended_action}), "
            "incomplete_sections (array of {section_number, missing_elements, recommendation}), "
            "coverage_notes (string)."
        )
        user_payload = (
            f"Guideline:\n{self.guideline_text}\n\n"
            f"Section 2.6 combined content:\n{truncated}"
        )
        try:
            response = self._client.responses.create(
                model=self.config.gap_model,
                input=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_payload},
                ],
                temperature=0.2,
                timeout=self.config.llm_timeout,
            )
        except Exception as exc:  # pragma: no cover - network/API failures
            logger.warning("Gap analysis request failed: %s", exc)
            raise

        raw_text = _extract_text_from_response(response)
        parsed: Dict[str, Any]
        try:
            parsed = json.loads(raw_text)
        except Exception:
            parsed = {"coverage_notes": raw_text}
        return {
            "model": self.config.gap_model,
            "raw_text": raw_text,
            "structured": parsed,
        }

    def run_gap_analysis_chunked(self, chunks: List[Dict[str, str]]) -> Dict[str, Any]:
        """Run gap analysis per chunk and merge results."""
        all_results: List[Dict[str, Any]] = []
        logger.info("Running gap analysis across %d chunk(s)", len(chunks))
        for chunk in chunks:
            content = chunk.get("content") or ""
            title = chunk.get("title") or "Chunk"
            try:
                start = time.monotonic()
                result = self.run_gap_analysis(f"{title}\n\n{content}")
                elapsed = time.monotonic() - start
                logger.info("Gap analysis completed for %s (%.1fs)", title, elapsed)
                result["chunk_title"] = title
                all_results.append(result)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Gap analysis failed for chunk %s: %s", title, exc)
                continue

        merged = _merge_gap_structured([r.get("structured") for r in all_results if r.get("structured")])
        merged = _validate_gap_structured(merged)
        gap_validation = _score_gap_validation(
            merged,
            chunk_count=len(chunks),
        )
        return {
            "model": self.config.gap_model,
            "raw_text": "\n\n".join(r.get("raw_text", "") for r in all_results if r.get("raw_text")),
            "structured": merged,
            "chunk_results": all_results,
            "validation": gap_validation,
        }

    def generate_missing_sections(
        self, missing_sections: List[Dict[str, Any]], context_text: str
    ) -> List[Dict[str, str]]:
        """Draft concise prose for missing sections using condensed context."""
        if not self._client or not missing_sections:
            return []
        prompt = (
            "You are a nonclinical regulatory writer. Using the provided Section 2.6 context, "
            "draft concise prose (<=2 paragraphs) for each missing section. "
            "If context is insufficient, return a clear placeholder like [MISSING: reason]. "
            "Respond ONLY with minified JSON array of objects: "
            '{"section_number": "...", "section_name": "...", "generated_text": "..."}'
        )
        truncated = _truncate_text(context_text, self.config.max_summary_chars, label="Context for autofill")
        try:
            start = time.monotonic()
            response = self._client.chat.completions.create(
                model=self.config.summary_model_override or self.config.gap_model or "gpt-4o-mini",
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"missing_sections": missing_sections, "context": truncated},
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0.2,
                timeout=self.config.llm_timeout,
            )
            elapsed = time.monotonic() - start
            logger.info("Auto-generated missing sections in %.1fs", elapsed)
            msg = response.choices[0].message if response.choices else None
            content = (msg.content or "") if msg else ""
            parsed = _safe_json_parse(content)
            if isinstance(parsed, list):
                return [
                    {
                        "section_number": str(item.get("section_number") or "").strip() or "N/A",
                        "section_name": str(item.get("section_name") or "").strip(),
                        "generated_text": str(item.get("generated_text") or "").strip(),
                    }
                    for item in parsed
                    if isinstance(item, dict)
                ]
            return []
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Auto-generation for missing sections failed: %s", exc)
            return []

    def generate_summary(
        self,
        combined_markdown: str,
        gap_payload: Dict[str, Any] | None,
        reference_examples: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        if not self._client:
            raise RuntimeError("OpenAI client is not available for summary generation.")
        template = dict(self.template_payload)
        model = self.config.summary_model_override or template.get("model") or "gpt-4-turbo-preview"
        temperature = template.get("temperature", 0.3)
        truncated = _truncate_text(
            combined_markdown, self.config.max_summary_chars, label="Section 2.6 content"
        )
        messages = []
        for message in template.get("messages", []):
            content = message.get("content", "")
            content = content.replace("{SCANNED_CONTENT}", truncated)
            if gap_payload:
                content += "\n\nGAP ANALYSIS (machine detected):\n"
                content += json.dumps(gap_payload, indent=2)
            if reference_examples:
                refs = "\n\n".join(
                    f"EXAMPLE {idx+1} ({ref.get('key','')}):\n{ref.get('text','')[: self.config.reference_summary_token_limit*4]}"
                    for idx, ref in enumerate(reference_examples)
                )
                content += "\n\nREFERENCE SUMMARIES (few-shot style cues):\n"
                content += refs
            messages.append({"role": message.get("role", "user"), "content": content})

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if "functions" in template:
            kwargs["functions"] = template["functions"]
        if "function_call" in template:
            kwargs["function_call"] = template["function_call"]
        if "response_format" in template:
            kwargs["response_format"] = template["response_format"]

        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # pragma: no cover - network/API failures
            logger.warning("2.4 summary generation failed: %s", exc)
            raise

        summary_payload = self._normalise_summary_response(response)
        return {
            "model": model,
            "raw_response": response.model_dump() if hasattr(response, "model_dump") else None,
            "summary": summary_payload,
        }

    def summarize_chunks(self, chunks: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Produce compact summaries per chunk to enable map->reduce assembly."""
        if not self._client:
            logger.warning("OpenAI client unavailable; chunk summarization skipped.")
            return []

        results: List[Dict[str, str]] = []
        model = self.config.summary_model_override or self.config.gap_model or "gpt-4o-mini"
        logger.info("Summarizing %d chunk(s) for map->reduce", len(chunks))
        for idx, chunk in enumerate(chunks, start=1):
            title = chunk.get("title") or f"Chunk {idx}"
            content = chunk.get("content") or ""
            truncated = _truncate_text(content, self.config.max_summary_chars, label=f"{title} content")
            prompt = (
                "You summarize nonclinical Section 2.6 content into concise bullet points. "
                "Return 4-8 bullets covering primary findings, PK, tox, and any gaps/unknowns. "
                "Keep it under 1200 characters. Do not include markdown links."
            )
            try:
                start = time.monotonic()
                response = self._client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": f"{title}\n\n{truncated}"},
                    ],
                    temperature=0.2,
                    timeout=self.config.llm_timeout,
                )
                elapsed = time.monotonic() - start
                msg = response.choices[0].message
                summary_text = (msg.content or "").strip() if msg else ""
                results.append({"title": title, "summary": summary_text})
                logger.info("Chunk summary completed for %s (%.1fs)", title, elapsed)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Chunk summary failed for %s: %s", title, exc)
        return results

    # ------------------------------------------------------------------
    @staticmethod
    def _load_guideline(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except Exception as exc:  # pragma: no cover - missing file
            logger.warning("Guideline file %s could not be read: %s", path, exc)
            return ""

    @staticmethod
    def _load_template(path: Path) -> Dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - missing file
            logger.warning("Template file %s could not be read: %s", path, exc)
            return {}

    @staticmethod
    def _normalise_summary_response(response: Any) -> Dict[str, Any]:
        choice = response.choices[0] if getattr(response, "choices", None) else None
        if not choice:
            return {}
        message = getattr(choice, "message", None)
        if message is None and isinstance(choice, dict):
            message = choice.get("message")
        function_call = getattr(message, "function_call", None) if message is not None else None
        if function_call is None and isinstance(message, dict):
            function_call = message.get("function_call")
        if function_call is None:
            function_call = {}
        arguments = getattr(function_call, "arguments", None)
        if hasattr(function_call, "model_dump") and not arguments:
            arguments = function_call.model_dump().get("arguments")
        if arguments is None and isinstance(function_call, dict):
            arguments = function_call.get("arguments")
        if arguments:
            arg_text = arguments if isinstance(arguments, str) else json.dumps(arguments)
            try:
                return json.loads(arg_text)
            except Exception:
                return {"raw_function_arguments": arg_text}
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if content:
            try:
                return json.loads(content)
            except Exception:
                return {"raw_content": content}
        return {}


class IND24GenerationPipeline:
    """High-level orchestrator that pulls Section 2.6, runs gap analysis, and produces Section 2.4."""

    def __init__(self, config: IND24GenerationConfig) -> None:
        self.config = config
        self._collector = Section26MarkdownCollector(config)
        self._llm = IND24LLMClient(config)

    def run(self) -> Dict[str, Any]:
        documents, combined_markdown, output_prefix = self._collector.collect()
        if not combined_markdown:
            raise RuntimeError("No Section 2.6 markdown could be collected.")

        chunks = _chunk_markdown(
            combined_markdown,
            self.config.max_gap_tokens_per_chunk,
            self.config.max_chunks,
        )
        logger.info(
            "Prepared %d chunk(s) for processing (token budget per chunk=%d, max_chunks=%d)",
            len(chunks),
            self.config.max_gap_tokens_per_chunk,
            self.config.max_chunks,
        )
        chunk_summaries = self._llm.summarize_chunks(chunks)
        gap_result = self._llm.run_gap_analysis_chunked(chunks)
        gap_structured = gap_result.get("structured")

        condensed_input = _build_condensed_summary_input(chunk_summaries)
        reference_examples = self._collector.fetch_reference_summaries()
        auto_generated: List[Dict[str, str]] = []
        missing_sections = (
            gap_structured.get("missing_sections") if isinstance(gap_structured, dict) else None
        ) or []
        if missing_sections:
            auto_generated = self._llm.generate_missing_sections(
                missing_sections, condensed_input or combined_markdown
            )
        gap_result["auto_generated_sections"] = auto_generated

        summary_result = self._llm.generate_summary(
            condensed_input, gap_structured, reference_examples=reference_examples
        )
        if reference_examples:
            summary_result["reference_examples"] = [ref.get("key", "") for ref in reference_examples]
        summary_validation = _score_summary_validation(summary_result)
        summary_result["validation"] = summary_validation

        outputs = self._persist_outputs(
            output_prefix=output_prefix,
            combined_markdown=combined_markdown,
            gap_result=gap_result,
            summary_result=summary_result,
        )
        return {
            "documents": documents,
            "gap_analysis": gap_result,
            "summary": summary_result,
            "output_keys": outputs,
        }

    # ------------------------------------------------------------------
    def _persist_outputs(
        self,
        *,
        output_prefix: str,
        combined_markdown: str,
        gap_result: Dict[str, Any],
        summary_result: Dict[str, Any],
    ) -> Dict[str, str]:
        s3_client = self._collector._s3
        combined_key = self.config.output_combined_markdown_key or f"{output_prefix}/section_2_6_combined.md"
        gap_key = self.config.output_gap_key or f"{output_prefix}/section_2_6_gap_analysis.md"
        gap_json_key = self.config.output_gap_json_key or f"{output_prefix}/section_2_6_gap_analysis.json"
        summary_key = self.config.output_summary_key or f"{output_prefix}/section_2_4_summary.md"
        summary_json_key = self.config.output_summary_json_key or f"{output_prefix}/section_2_4_summary.json"

        gap_markdown = _format_gap_markdown(gap_result)
        summary_markdown = _format_summary_markdown(summary_result)

        s3_client.put_object(
            Bucket=self.config.bucket,
            Key=combined_key,
            Body=combined_markdown.encode("utf-8"),
            ContentType="text/markdown",
        )
        s3_client.put_object(
            Bucket=self.config.bucket,
            Key=gap_key,
            Body=gap_markdown.encode("utf-8"),
            ContentType="text/markdown",
        )
        s3_client.put_object(
            Bucket=self.config.bucket,
            Key=gap_json_key,
            Body=json.dumps(gap_result, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        s3_client.put_object(
            Bucket=self.config.bucket,
            Key=summary_key,
            Body=summary_markdown.encode("utf-8"),
            ContentType="text/markdown",
        )
        s3_client.put_object(
            Bucket=self.config.bucket,
            Key=summary_json_key,
            Body=json.dumps(summary_result, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        return {
            "combined_markdown_key": combined_key,
            "gap_analysis_key": gap_key,
            "gap_analysis_json_key": gap_json_key,
            "summary_key": summary_key,
            "summary_json_key": summary_json_key,
        }


def _slugify(value: str) -> str:
    lowered = value.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slug.strip("-") or "unknown"


def _truncate_text(text: str, limit: int, label: str) -> str:
    """
    Truncate a large text blob to a safe character limit for LLM context windows.
    Adds a short header so the model knows the content is truncated.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    logger.warning("%s truncated from %d to %d characters for LLM input.", label, len(text), limit)
    return f"[TRUNCATED to first {limit} chars of {len(text)} {label}]\n\n{text[:limit]}"


def _format_gap_markdown(gap_result: Dict[str, Any]) -> str:
    header = ["# Section 2.6 Gap Analysis"]
    model = gap_result.get("model")
    if model:
        header.append(f"_Model: {model}_")
    structured = gap_result.get("structured")
    validation = gap_result.get("validation") or {}
    if validation:
        header.append("\n## Validation")
        header.append(f"- Confidence: {validation.get('confidence', 'N/A')}")
        issues = validation.get("issues") or []
        if issues:
            header.append("- Issues:")
            for issue in issues:
                header.append(f"  - {issue}")

    if isinstance(structured, dict) and structured:
        missing = structured.get("missing_sections") or []
        incomplete = structured.get("incomplete_sections") or []
        coverage = structured.get("coverage_notes") or ""

        if missing:
            header.append("\n## Missing Sections")
            for item in missing:
                section = item.get("section_number") or "N/A"
                name = item.get("section_name") or ""
                severity = item.get("severity") or ""
                desc = item.get("description") or ""
                rec = item.get("recommended_action") or item.get("regulatory_impact") or ""
                header.append(f"- **{section} {name}** ({severity}): {desc} {rec}".strip())

        if incomplete:
            header.append("\n## Incomplete Sections")
            for item in incomplete:
                section = item.get("section_number") or "N/A"
                missing_elems = item.get("missing_elements") or []
                rec = item.get("recommendation") or ""
                header.append(f"- **{section}** missing: {', '.join(missing_elems)}. {rec}".strip())

        if coverage:
            header.append("\n## Coverage Notes")
            header.append(str(coverage))

        if not missing and not incomplete and not coverage:
            header.append("\n_No structured gaps returned; see raw text below._")

    auto_gen = gap_result.get("auto_generated_sections") or []
    if auto_gen:
        header.append("\n## Auto-Generated Drafts for Missing Sections")
        for item in auto_gen:
            section = item.get("section_number") or "N/A"
            name = item.get("section_name") or ""
            text = item.get("generated_text") or ""
            header.append(f"### {section} {name}".strip())
            header.append(text.strip() or "[MISSING: Generation returned empty text]")

    return "\n".join(header).strip()


def _format_summary_markdown(summary_result: Dict[str, Any]) -> str:
    header = ["# Section 2.4 Summary"]
    model = summary_result.get("model")
    if model:
        header.append(f"_Model: {model}_")

    summary = summary_result.get("summary")
    if isinstance(summary, dict) and summary:
        # Document metadata
        meta = summary.get("document_metadata") or {}
        if meta:
            header.append("\n## Document Metadata")
            for key, value in meta.items():
                header.append(f"- **{key}**: {value}")

        # Gap highlights
        gap = summary.get("gap_analysis") or {}
        missing = gap.get("missing_sections") or []
        incomplete = gap.get("incomplete_sections") or []
        if missing or incomplete:
            header.append("\n## Gap Highlights (from summary)")
            for item in missing:
                section = item.get("section_number") or "N/A"
                name = item.get("section_name") or ""
                severity = item.get("severity") or ""
                desc = item.get("description") or ""
                header.append(f"- **{section} {name}** ({severity}): {desc}".strip())
            for item in incomplete:
                section = item.get("section_number") or "N/A"
                elems = item.get("missing_elements") or []
                rec = item.get("recommendation") or ""
                header.append(f"- **{section}** incomplete: {', '.join(elems)}. {rec}".strip())

        # Section 2.4 content
        content = summary.get("section_2_4_content") or {}
        if content:
            header.append("\n## Section 2.4 Content")
            for section_key, section_val in content.items():
                header.append(f"\n### {section_key}")
                header.extend(_render_section_content(section_val))

        # Summary statistics
        stats = summary.get("summary_statistics") or {}
        if stats:
            header.append("\n## Summary Statistics")
            for key, value in stats.items():
                header.append(f"- **{key}**: {value}")
    validation = summary_result.get("validation") or {}
    if validation:
        header.append("\n## Validation")
        header.append(f"- Confidence: {validation.get('confidence', 'N/A')}")
        issues = validation.get("issues") or []
        if issues:
            header.append("- Issues:")
            for issue in issues:
                header.append(f"  - {issue}")
    else:
        # Fallback: raw JSON/text
        if summary:
            header.append("\n## Summary (raw)")
            header.append("```json")
            header.append(json.dumps(summary, indent=2))
            header.append("```")

    raw_response = summary_result.get("raw_response")
    if raw_response:
        header.append("\n## Raw Response (truncated)")
        header.append("```json")
        try:
            header.append(json.dumps(raw_response, indent=2)[:8000])
        except Exception:
            header.append(str(raw_response)[:8000])
        header.append("```")

    return "\n".join(header).strip()


def _render_section_content(value: Any) -> list[str]:
    lines: list[str] = []
    if isinstance(value, str):
        lines.append(value)
    elif isinstance(value, dict):
        for key, val in value.items():
            if isinstance(val, (str, int, float)):
                lines.append(f"- **{key}**: {val}")
            elif isinstance(val, list):
                lines.append(f"- **{key}**:")
                for item in val:
                    if isinstance(item, (str, int, float)):
                        lines.append(f"  - {item}")
                    elif isinstance(item, dict):
                        flat = "; ".join(f"{k}={v}" for k, v in item.items())
                        lines.append(f"  - {flat}")
                    else:
                        lines.append(f"  - {json.dumps(item)}")
            elif isinstance(val, dict):
                lines.append(f"- **{key}**:")
                for sub_k, sub_v in val.items():
                    lines.append(f"  - {sub_k}: {sub_v}")
            else:
                lines.append(f"- **{key}**: {json.dumps(val)}")
    else:
        lines.append(str(value))
    return lines


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 4))


def _chunk_markdown(markdown: str, token_limit: int, max_chunks: int) -> List[Dict[str, str]]:
    """
    Split markdown into chunks by headings while enforcing a soft token limit.
    """
    lines = markdown.splitlines()
    chunks: List[Dict[str, str]] = []
    current: List[str] = []
    title = "Chunk 1"
    current_tokens = 0
    chunk_idx = 1
    heading_pattern = re.compile(r"^#{1,3}\s+(.*)")

    def flush() -> None:
        nonlocal chunk_idx, current, current_tokens, title
        content = "\n".join(current).strip()
        if content:
            if max_chunks > 0 and len(chunks) >= max_chunks:
                # Append overflow to the last chunk to avoid dropping content.
                chunks[-1]["content"] = f"{chunks[-1]['content']}\n\n{content}"
            else:
                chunks.append({"title": title, "content": content})
        chunk_idx += 1
        title = f"Chunk {chunk_idx}"
        current = []
        current_tokens = 0

    for line in lines:
        heading_match = heading_pattern.match(line)
        line_tokens = _estimate_tokens(line + "\n")
        if heading_match and current and (current_tokens + line_tokens) > token_limit:
            flush()
            title = heading_match.group(1).strip() or title
            current.append(line)
            current_tokens = line_tokens
            continue

        if (current_tokens + line_tokens) > token_limit and current:
            flush()
        if heading_match and not current:
            title = heading_match.group(1).strip() or title
        current.append(line)
        current_tokens += line_tokens

    if current:
        flush()
    return chunks or [{"title": "Chunk 1", "content": markdown}]


def _merge_gap_structured(payloads: Iterable[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    severity_rank = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}
    missing_map: Dict[str, Dict[str, Any]] = {}
    incomplete_map: Dict[str, Dict[str, Any]] = {}

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for item in payload.get("missing_sections") or []:
            key = f"{item.get('section_number','')}::{item.get('section_name','')}".strip(":")
            current = missing_map.get(key)
            severity = item.get("severity") or ""
            rank = severity_rank.get(severity, 0)
            if current:
                existing_rank = severity_rank.get(current.get("severity") or "", 0)
                if rank > existing_rank:
                    current["severity"] = severity
                desc = item.get("description")
                if desc and desc not in current.get("description", ""):
                    current["description"] = f"{current.get('description','')}; {desc}".strip("; ")
                continue
            missing_map[key] = dict(item)

        for item in payload.get("incomplete_sections") or []:
            key = item.get("section_number") or ""
            if key in incomplete_map:
                existing = incomplete_map[key]
                merged_missing = list(existing.get("missing_elements") or [])
                for elem in item.get("missing_elements") or []:
                    if elem not in merged_missing:
                        merged_missing.append(elem)
                existing["missing_elements"] = merged_missing
                rec = item.get("recommendation")
                if rec and rec not in (existing.get("recommendation") or ""):
                    existing["recommendation"] = f"{existing.get('recommendation','')}; {rec}".strip("; ")
                continue
            incomplete_map[key] = dict(item)

    return {
        "missing_sections": list(missing_map.values()),
        "incomplete_sections": list(incomplete_map.values()),
    }


def _build_condensed_summary_input(chunk_summaries: List[Dict[str, str]]) -> str:
    if not chunk_summaries:
        return ""
    blocks = []
    for idx, chunk in enumerate(chunk_summaries, start=1):
        title = chunk.get("title") or f"Chunk {idx}"
        summary = chunk.get("summary") or ""
        blocks.append(f"## {title}\n{summary}")
    return "\n\n".join(blocks)


def _validate_gap_structured(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize and validate gap analysis structure; de-duplicate entries."""
    result: Dict[str, Any] = {"missing_sections": [], "incomplete_sections": []}
    seen_missing: set[tuple[str, str]] = set()
    for item in payload.get("missing_sections") or []:
        if not isinstance(item, dict):
            continue
        sec = str(item.get("section_number") or "").strip()
        name = str(item.get("section_name") or "").strip()
        key = (sec, name)
        if key in seen_missing:
            continue
        seen_missing.add(key)
        cleaned = {
            "section_number": sec or "N/A",
            "section_name": name,
            "severity": str(item.get("severity") or "").upper() or "UNSPECIFIED",
            "description": str(item.get("description") or "").strip(),
            "regulatory_impact": str(item.get("regulatory_impact") or "").strip(),
        }
        result["missing_sections"].append(cleaned)

    seen_incomplete: set[str] = set()
    for item in payload.get("incomplete_sections") or []:
        if not isinstance(item, dict):
            continue
        sec = str(item.get("section_number") or "").strip()
        if sec in seen_incomplete:
            continue
        seen_incomplete.add(sec)
        missing_elems = item.get("missing_elements") or []
        if not isinstance(missing_elems, list):
            missing_elems = [str(missing_elems)]
        cleaned = {
            "section_number": sec or "N/A",
            "missing_elements": [str(elem).strip() for elem in missing_elems if str(elem).strip()],
            "recommendation": str(item.get("recommendation") or "").strip(),
        }
        result["incomplete_sections"].append(cleaned)
    return result


def _score_gap_validation(payload: Dict[str, Any], chunk_count: int) -> Dict[str, Any]:
    missing = payload.get("missing_sections") or []
    incomplete = payload.get("incomplete_sections") or []
    issues: List[str] = []
    evidence: List[str] = []
    missing_count = len(missing)
    incomplete_count = len(incomplete)
    severity_counts: Dict[str, int] = {}
    for item in missing:
        sev = str(item.get("severity") or "").upper()
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    if severity_counts:
        evidence.append(
            "Missing by severity: "
            + ", ".join(f"{k or 'UNSPECIFIED'}={v}" for k, v in sorted(severity_counts.items()))
        )
    if chunk_count:
        evidence.append(f"Chunks analyzed: {chunk_count}")
    if missing_count == 0 and incomplete_count == 0:
        issues.append("No missing or incomplete sections detected.")
    else:
        if missing_count:
            issues.append(f"{missing_count} missing sections flagged.")
        if incomplete_count:
            issues.append(f"{incomplete_count} incomplete sections flagged.")

    confidence = 0.9
    confidence -= 0.08 * missing_count
    confidence -= 0.05 * incomplete_count
    confidence += 0.01 * max(chunk_count, 1)
    confidence = _clamp(confidence, 0.0, 0.99)
    return {"confidence": round(confidence, 3), "issues": issues, "evidence": evidence}


def _score_summary_validation(summary_result: Dict[str, Any]) -> Dict[str, Any]:
    summary = summary_result.get("summary") if isinstance(summary_result, dict) else None
    issues: List[str] = []
    evidence: List[str] = []
    ref_examples = summary_result.get("reference_examples") or []
    if ref_examples:
        evidence.append(f"Reference summaries used: {len(ref_examples)}")
    placeholders = 0
    required_sections = [
        "2.4.1_introduction",
        "2.4.2_pharmacology_summary",
        "2.4.3_pharmacokinetics_summary",
        "2.4.4_toxicology_summary",
        "2.4.5_integrated_risk_assessment",
    ]
    present = set()
    if isinstance(summary, dict):
        content = summary.get("section_2_4_content") or {}
        present.update(content.keys())
        placeholders = _count_placeholders(summary)
    missing_sections = [sec for sec in required_sections if sec not in present]
    if missing_sections:
        issues.append(f"Missing sections: {', '.join(missing_sections)}")
    if placeholders:
        issues.append(f"Placeholders detected: {placeholders}")
    if present:
        evidence.append(f"Sections present: {', '.join(sorted(present))}")
    evidence.append(f"Placeholders counted: {placeholders}")

    confidence = 0.9
    if isinstance(summary, dict):
        meta = summary.get("document_metadata") or {}
        if "completeness_score" in meta:
            try:
                confidence = max(confidence, float(meta["completeness_score"]) / 100.0)
                evidence.append(f"Completeness score: {meta['completeness_score']}")
            except Exception:
                pass
    confidence -= 0.1 * len(missing_sections)
    confidence -= 0.05 * placeholders
    confidence = _clamp(confidence, 0.0, 0.99)
    return {"confidence": round(confidence, 3), "issues": issues, "evidence": evidence}


def _safe_json_parse(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _count_placeholders(obj: Any) -> int:
    pattern = re.compile(r"\[MISSING:", re.IGNORECASE)
    def _iter_strings(val: Any) -> Iterable[str]:
        if isinstance(val, str):
            yield val
        elif isinstance(val, dict):
            for v in val.values():
                yield from _iter_strings(v)
        elif isinstance(val, list):
            for v in val:
                yield from _iter_strings(v)
    return sum(1 for s in _iter_strings(obj) if pattern.search(s))


def validate_gap_payload(payload: Dict[str, Any], chunk_count: int = 1) -> Dict[str, Any]:
    """Public helper to validate gap analysis payloads."""
    structured = _validate_gap_structured(payload or {})
    return _score_gap_validation(structured, chunk_count)


def validate_summary_payload(summary_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Public helper to validate summary payloads (expects full summary_result or summary dict).
    """
    if summary_payload and "summary" in summary_payload:
        target = summary_payload
    else:
        target = {"summary": summary_payload}
    return _score_summary_validation(target)
