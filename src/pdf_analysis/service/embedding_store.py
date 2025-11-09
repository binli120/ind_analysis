"""Embedding store integrations for Supabase/pgvector and OpenAI generation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:
    import httpx  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    httpx = None  # type: ignore

try:
    from openai import OpenAI
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore[misc]

try:
    import psycopg  # type: ignore
    from psycopg import sql  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    psycopg = None  # type: ignore
    sql = None  # type: ignore

logger = logging.getLogger(__name__)


class SupabaseEmbeddingStore:
    """
    Generates embeddings for document payloads and upserts them into a Supabase pgvector table.
    """

    def __init__(
        self,
        *,
        table: str = "ind_docs",
        embedding_model: str = "text-embedding-3-small",
        max_chars: int = 9000,
        timeout_seconds: float = 30.0,
        on_conflict: Optional[str] = None,
    ) -> None:
        self.table = table
        self.embedding_model = embedding_model
        self.max_chars = max_chars
        self.timeout_seconds = timeout_seconds

        load_dotenv = self._resolve_dotenv()
        if load_dotenv:
            for candidate in (".env.local", ".env"):
                env_path = Path(candidate)
                if env_path.exists():
                    load_dotenv(dotenv_path=env_path, override=False)

        self.api_key = os.getenv("OPENAI_API_KEY")
        self.supabase_url = (
            os.getenv("SUPABASE_URL")
            or os.getenv("SUPABASE_PROJECT_URL")
            or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
        )
        self.supabase_key = (
            os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            or os.getenv("SUPABASE_ANON_KEY")
            or os.getenv("SUPABASE_API_KEY")
            or os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY")
        )

        if OpenAI is None:
            logger.warning("OpenAI client unavailable; install the 'infra' extras to enable embeddings.")
            self._openai_client: Optional[OpenAI] = None
        elif not self.api_key:
            logger.warning("OPENAI_API_KEY is not configured; skipping embedding generation.")
            self._openai_client = None
        else:
            self._openai_client = OpenAIMetadataClientWrapper(api_key=self.api_key)

        env_on_conflict = on_conflict if on_conflict is not None else os.getenv("SUPABASE_ON_CONFLICT")
        if isinstance(env_on_conflict, str):
            env_on_conflict = env_on_conflict.strip()
        self.on_conflict = (
            env_on_conflict if env_on_conflict else "document_hash"
        )

        if httpx is None:
            logger.warning("httpx is not installed; Supabase embedding storage is disabled.")
            self._http_client: Optional[httpx.Client] = None  # type: ignore[attr-defined]
        elif not self.supabase_url or not self.supabase_key:
            logger.warning(
                "Supabase credentials missing (set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY)."
            )
            self._http_client = None  # type: ignore[attr-defined]
        else:
            self._http_client = httpx.Client(
                base_url=self.supabase_url.rstrip("/"),
                headers={
                    "apikey": self.supabase_key,
                    "Authorization": f"Bearer {self.supabase_key}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                timeout=self.timeout_seconds,
            )

        self.db_dsn = (
            os.getenv("SUPABASE_DB_URL")
            or os.getenv("SUPABASE_DB_CONNECTION")
            or os.getenv("SUPABASE_CONNECTION_STRING")
        )
        if self.db_dsn and psycopg is None:  # pragma: no cover - optional dependency
            logger.warning("psycopg is not installed; similarity search will be unavailable.")
            self.db_dsn = None

    @staticmethod
    def _resolve_dotenv():
        try:
            from dotenv import load_dotenv  # type: ignore

            return load_dotenv
        except ModuleNotFoundError:  # pragma: no cover - optional dependency
            return None

    def is_available(self) -> bool:
        return bool(self._openai_client and self._http_client)

    def store_document(
        self,
        *,
        s3_bucket: str,
        s3_key: str,
        version_id: Optional[str],
        filename: str,
        markdown: str,
        metadata: Dict[str, Any],
        company: Optional[str] = None,
        project: Optional[str] = None,
        module_label: Optional[str] = None,
        module_number: Optional[int] = None,
        labels: Optional[Sequence[str]] = None,
        keywords: Optional[Sequence[str]] = None,
        language: Optional[str] = None,
    ) -> bool:
        if not self.is_available():
            return False
        if not markdown:
            return False
        text = self._build_embedding_text(
            markdown=markdown,
            metadata=metadata,
            filename=filename,
            company=company,
            project=project,
            module_label=module_label,
        )
        if not text:
            return False

        embedding = self._generate_embedding(text)
        if not embedding:
            return False

        labels = labels or _ensure_list(metadata.get("labels"))
        keywords = keywords or _ensure_list(metadata.get("keywords"))
        language = language or metadata.get("language")
        row = self._build_row(
            s3_bucket=s3_bucket,
            s3_key=s3_key,
            version_id=version_id,
            filename=filename,
            metadata=metadata,
            company=company,
            project=project,
            module_label=module_label,
            module_number=module_number,
            labels=labels,
            keywords=keywords,
            language=language,
            embedding=embedding,
            markdown=text,
            document_hash=_compute_document_hash(
                s3_bucket=s3_bucket,
                s3_key=s3_key,
                version_id=version_id,
                content=text,
            ),
            )
        return self._upsert(row)

    # ------------------------------------------------------------------
    def search_similar_text(
        self,
        text: str,
        *,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        if not text.strip():
            return []
        embedding = self._generate_embedding(text[: self.max_chars])
        if not embedding:
            return []
        return self.search_similar_embedding(embedding, top_k=top_k)

    def search_similar_embedding(
        self,
        embedding: Sequence[float],
        *,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        if not self.db_dsn:
            logger.warning("Supabase DB connection not configured; skipping similarity search.")
            return []
        if psycopg is None or sql is None:  # pragma: no cover - optional dependency
            return []
        vector_literal = _vector_literal(embedding)
        try:
            with psycopg.connect(self.db_dsn, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    query = sql.SQL(
                        """
                        SELECT
                            id,
                            company,
                            filename,
                            section,
                            content,
                            metadata,
                            embedding_1536 <=> %s::vector AS distance
                        FROM {table}
                        ORDER BY embedding_1536 <=> %s::vector
                        LIMIT %s
                        """
                    ).format(table=psycopg.sql.Identifier(self.table))
                    cur.execute(query, (vector_literal, vector_literal, top_k))
                    records = cur.fetchall()
        except Exception as exc:  # pragma: no cover - network/db failure
            logger.warning("Similarity search failed: %s", exc)
            return []

        results: List[Dict[str, Any]] = []
        for row in records:
            (
                doc_id,
                company,
                filename,
                section,
                content,
                metadata,
                distance,
            ) = row
            results.append(
                {
                    "id": doc_id,
                    "company": company,
                    "filename": filename,
                    "section": section,
                    "content": content,
                    "metadata": metadata,
                    "distance": float(distance) if distance is not None else None,
                }
            )
        return results

    # ------------------------------------------------------------------
    def _generate_embedding(self, text: str) -> Optional[List[float]]:
        if not self._openai_client:
            return None
        try:
            result = self._openai_client.embeddings(model=self.embedding_model, input=text)
        except Exception as exc:  # pragma: no cover - network/API failure
            logger.warning("Failed to generate embedding: %s", exc)
            return None
        embedding = result.get("embedding")
        if not embedding:
            logger.warning("Embedding response did not contain data.")
            return None
        return embedding

    def _upsert(self, row: Dict[str, Any]) -> bool:
        if not self._http_client:
            return False
        try:
            params = {"on_conflict": self.on_conflict} if self.on_conflict else None
            response = self._http_client.post(
                f"/rest/v1/{self.table}",
                params=params,
                content=json.dumps([row]),
            )
        except Exception as exc:  # pragma: no cover - network failure
            logger.warning("Failed to upsert embedding for %s: %s", row.get("filename"), exc)
            return False

        if response.status_code >= 400:
            logger.warning(
                "Supabase upsert failed for %s: %s %s",
                row.get("filename"),
                response.status_code,
                response.text,
            )
            return False
        return True

    def _build_embedding_text(
        self,
        *,
        markdown: str,
        metadata: Dict[str, Any],
        filename: str,
        company: Optional[str],
        project: Optional[str],
        module_label: Optional[str],
    ) -> str:
        sections: List[str] = []
        sections.append(f"Document: {filename}")
        if company:
            sections.append(f"Company: {company}")
        if project:
            sections.append(f"Project: {project}")
        if module_label:
            sections.append(f"Module: {module_label}")

        ind_section = metadata.get("ind_section_number")
        if ind_section:
            title = metadata.get("ind_section_title") or ""
            sections.append(f"IND Section: {ind_section} {title}".strip())

        labels = _ensure_list(metadata.get("labels"))
        if labels:
            sections.append("Labels: " + ", ".join(labels))
        keywords = _ensure_list(metadata.get("keywords"))
        if keywords:
            sections.append("Keywords: " + ", ".join(keywords))

        summary = metadata.get("summary") or metadata.get("quality_markdown")
        if summary:
            sections.append(f"Summary:\n{summary}")

        sections.append(markdown.strip())
        combined = "\n\n".join(sections).strip()
        return combined[: self.max_chars]

    def _build_row(
        self,
        *,
        s3_bucket: str,
        s3_key: str,
        version_id: Optional[str],
        filename: str,
        metadata: Dict[str, Any],
        company: Optional[str],
        project: Optional[str],
        module_label: Optional[str],
        module_number: Optional[int],
        labels: Optional[Sequence[str]],
        keywords: Optional[Sequence[str]],
        language: Optional[str],
        embedding: Sequence[float],
        markdown: str,
        document_hash: str,
    ) -> Dict[str, Any]:
        section = metadata.get("ind_section_number")
        if not section:
            module_part = module_label or ""
            if module_number is not None:
                section = f"Module {module_number}"
            elif module_part:
                section = module_part
            else:
                section = "Unknown"

        row: Dict[str, Any] = {
            "company": company,
            "filename": filename,
            "document_hash": document_hash,
            "section": str(section),
            "content": markdown,
            "metadata": {
                "s3_bucket": s3_bucket,
                "s3_key": s3_key,
                "s3_version": version_id,
                "project": project,
                "module_label": module_label,
                "module_number": module_number,
                "labels": list(labels) if labels else [],
                "keywords": list(keywords) if keywords else [],
                "language": language,
                "ind_document_type": metadata.get("ind_document_type"),
                "ind_section_title": metadata.get("ind_section_title"),
                "ind_classification_confidence": metadata.get("ind_classification_confidence"),
            },
            "embedding_1536": [float(value) for value in embedding],
        }
        return row


class OpenAIMetadataClientWrapper:
    """
    Lightweight wrapper around OpenAI client to simplify dependency injection.
    """

    def __init__(self, api_key: str) -> None:
        self._client = OpenAI(api_key=api_key) if api_key else OpenAI()

    def embeddings(self, model: str, input: str) -> Dict[str, Any]:
        response = self._client.embeddings.create(model=model, input=input)
        if not response.data:
            return {}
        return {"embedding": response.data[0].embedding}


def _ensure_list(values: Any) -> List[str]:
    if isinstance(values, str):
        return [segment.strip() for segment in values.split(",") if segment.strip()]
    if isinstance(values, Iterable):
        output: List[str] = []
        for value in values:
            string_value = str(value).strip()
            if string_value:
                output.append(string_value)
        return output
    return []


def _compute_document_hash(
    *,
    s3_bucket: str,
    s3_key: str,
    version_id: Optional[str],
    content: str,
) -> str:
    hasher = hashlib.sha256()
    hasher.update(s3_bucket.encode("utf-8"))
    hasher.update(b":")
    hasher.update(s3_key.encode("utf-8"))
    hasher.update(b":")
    if version_id:
        hasher.update(version_id.encode("utf-8"))
    hasher.update(b":")
    hasher.update(content.encode("utf-8"))
    return hasher.hexdigest()


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(v):.8f}" for v in values) + "]"
