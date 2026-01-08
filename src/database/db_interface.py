# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""
Lightweight database interface for persisting Module 4 extraction artifacts into the NCD schema.

This wrapper aligns the pipeline outputs (pages, chunks, embeddings) with the tables defined in
`ncd-schema.sql`. It keeps write operations small and composable so the PDF pipeline can
persist results step by step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

import json
import re

from sqlalchemy import text as sqltext
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ncd.database.db import SessionLocal, engine as default_engine


@dataclass
class PageText:
    page_number: int
    text: str


@dataclass
class TextChunkRecord:
    """
    Represents a text chunk extracted from a document.
    """
    raw_text: str
    source_document_id: str
    page_from: Optional[int] = None
    page_to: Optional[int] = None
    offset_start: Optional[int] = None
    offset_end: Optional[int] = None
    section_label: Optional[str] = None
    study_id: Optional[str] = None
    embedding: Optional[Sequence[float]] = None


@dataclass
class DocumentAssetRecord:
    """Represents a document asset such as an image or table."""
    asset_type: str
    s3_bucket: str
    s3_key: str
    document_version_id: str
    page_number: Optional[int] = None
    index_on_page: Optional[int] = None
    caption: Optional[str] = None
    description: Optional[str] = None
    keywords: Optional[Sequence[str]] = None
    extra_attributes: Optional[dict] = None


@dataclass
class DocumentKeySectionRecord:
    """Represents a key section extracted from a document."""
    document_version_id: str
    section_type: str
    text: str
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    asset_ids: Optional[Sequence[str]] = None
    model_name: Optional[str] = None
    confidence: Optional[float] = None


@dataclass
class CTDSectionReferenceRecord:
    tenant_id: str
    project_id: str
    bucket: str
    element_number: str
    section_number: Optional[str] = None
    template_payload: Optional[dict] = None
    module4_sections: Optional[Sequence[str]] = None
    payload: Optional[dict] = None


@dataclass
class CTDSectionSummaryRecord:
    tenant_id: str
    project_id: str
    bucket: str
    section_number: str
    summary_text: str
    status: str = "draft"
    element_numbers: Optional[Sequence[str]] = None
    user_prompt: Optional[str] = None
    user_comment: Optional[str] = None
    previous_id: Optional[str] = None
    model_name: Optional[str] = None
    embedding: Optional[Sequence[float]] = None


@dataclass
class CTDTabulatedSummaryRecord:
    tenant_id: str
    project_id: str
    bucket: str
    section_number: str
    table_payload: dict
    status: str = "draft"
    user_prompt: Optional[str] = None
    user_comment: Optional[str] = None
    previous_id: Optional[str] = None
    model_name: Optional[str] = None
    embedding: Optional[Sequence[float]] = None


class NCDRepository:
    """Small helper for inserting document/pages/chunks into the NCD schema."""

    def __init__(
        self, engine: Engine | None = None, session: Session | None = None
    ) -> None:
        self._engine = engine or default_engine
        self._external_session = session
        self._session: Session | None = None
        self._documents_has_content: bool | None = None
        self._documents_embedding_info: dict | None = None
        self._table_exists_cache: dict[str, bool] = {}

    def __enter__(self) -> "NCDRepository":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def session(self) -> Session:
        if self._external_session:
            return self._external_session
        if self._session is None:
            self._session = SessionLocal()
        return self._session

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
        self._session = None

    def _table_exists(self, table_name: str) -> bool:
        cached = self._table_exists_cache.get(table_name)
        if cached is not None:
            return cached
        db = self.session
        exists = db.execute(
            sqltext("SELECT to_regclass(:table_name)"),
            {"table_name": table_name},
        ).scalar()
        present = exists is not None
        self._table_exists_cache[table_name] = present
        return present

    @staticmethod
    def _parse_duration_days(duration: str | None) -> Optional[int]:
        if not duration:
            return None
        match = re.search(r"(\d+)", duration)
        if not match:
            return None
        value = int(match.group(1))
        lowered = duration.lower()
        if "week" in lowered:
            return value * 7
        if "month" in lowered:
            return value * 30
        if "year" in lowered:
            return value * 365
        return value

    @staticmethod
    def _vector_literal(embedding: Sequence[float]) -> str:
        clean = [float(value) for value in embedding]
        return "[" + ",".join(f"{value:.6f}" for value in clean) + "]"

    # ------------------------------------------------------------------ #
    # Source document + pages
    # ------------------------------------------------------------------ #
    def ensure_document_and_version(
        self,
        *,
        tenant_id: str,
        title: str,
        s3_bucket: str,
        s3_key: str,
        s3_version_id: str | None,
        file_type: str,
        content_hash: str,
        page_count: int | None,
        created_by: str | None,
    ) -> tuple[str, str]:
        """
        Ensure a document + document_version row exists for the given S3 object.
        Returns (document_id, document_version_id).
        """
        db = self.session
        existing = db.execute(
            sqltext(
                """
                SELECT dv.id AS document_version_id, dv.document_id
                FROM document_versions dv
                JOIN documents d ON dv.document_id = d.id
                WHERE dv.s3_bucket = :bucket
                  AND dv.s3_key = :key
                  AND COALESCE(dv.s3_version_id, '') = COALESCE(:version_id, '')
                LIMIT 1
                """
            ),
            {"bucket": s3_bucket, "key": s3_key, "version_id": s3_version_id},
        ).first()
        if existing:
            return str(existing.document_id), str(existing.document_version_id)

        document_id = self._insert_document(
            tenant_id=tenant_id,
            title=title,
        )
        if document_id is None:
            raise RuntimeError("Failed to insert documents")

        document_version_id = db.execute(
            sqltext(
                """
                INSERT INTO document_versions (
                    document_id, s3_bucket, s3_key, s3_version_id,
                    file_type, content_hash, page_count, created_by
                )
                VALUES (
                    :document_id, :bucket, :key, :version_id,
                    :file_type, :content_hash, :page_count, :created_by
                )
                ON CONFLICT (document_id, content_hash)
                DO UPDATE SET
                    page_count = EXCLUDED.page_count,
                    s3_bucket = EXCLUDED.s3_bucket,
                    s3_key = EXCLUDED.s3_key,
                    s3_version_id = EXCLUDED.s3_version_id
                RETURNING id
                """
            ),
            {
                "document_id": document_id,
                "bucket": s3_bucket,
                "key": s3_key,
                "version_id": s3_version_id,
                "file_type": file_type,
                "content_hash": content_hash,
                "page_count": page_count,
                "created_by": created_by,
            },
        ).scalar()
        if document_version_id is None:
            raise RuntimeError("Failed to insert document_versions")
        db.commit()
        return chunk_ids
        return str(document_id), str(document_version_id)

    def _insert_document(self, *, tenant_id: str, title: str) -> str | None:
        db = self.session
        columns = ["tenant_id", "title"]
        values: dict[str, object] = {
            "tenant_id": tenant_id,
            "title": title,
        }
        if self._documents_has_content_column():
            columns.append("content")
            values["content"] = title

        embedding_payload = self._documents_embedding_payload()
        if embedding_payload is not None:
            columns.append("embedding")
            values["embedding"] = embedding_payload

        cols_sql = ", ".join(columns)
        params_sql = ", ".join(f":{col}" for col in columns)
        try:
            return db.execute(
                sqltext(
                    f"""
                    INSERT INTO documents ({cols_sql})
                    VALUES ({params_sql})
                    RETURNING id
                    """
                ),
                values,
            ).scalar()
        except Exception:
            db.rollback()
            raise

    def _documents_has_content_column(self) -> bool:
        if self._documents_has_content is not None:
            return self._documents_has_content
        db = self.session
        row = db.execute(
            sqltext(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'documents'
                  AND column_name = 'content'
                LIMIT 1
                """
            )
        ).scalar()
        self._documents_has_content = row is not None
        return self._documents_has_content

    def _documents_embedding_payload(self) -> object | None:
        info = self._documents_embedding_info_cached()
        if info is None:
            return None
        if info.get("has_default") or info.get("is_nullable"):
            return None
        return self._default_embedding_value(info)

    def _documents_embedding_info_cached(self) -> dict | None:
        if self._documents_embedding_info is not None:
            return self._documents_embedding_info
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                SELECT column_name, is_nullable, data_type, udt_name, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'documents'
                  AND column_name = 'embedding'
                LIMIT 1
                """
                )
            )
            .mappings()
            .first()
        )
        if not row:
            self._documents_embedding_info = None
            return None
        self._documents_embedding_info = {
            "is_nullable": row.get("is_nullable") == "YES",
            "data_type": row.get("data_type"),
            "udt_name": row.get("udt_name"),
            "has_default": row.get("column_default") is not None,
            "type_repr": self._documents_embedding_type_repr(),
        }
        return self._documents_embedding_info

    def _documents_embedding_type_repr(self) -> str | None:
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                SELECT format_type(a.atttypid, a.atttypmod) AS type_repr
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname = 'documents'
                  AND a.attname = 'embedding'
                  AND a.attnum > 0
                LIMIT 1
                """
                )
            )
            .mappings()
            .first()
        )
        return str(row.get("type_repr")) if row else None

    def _default_embedding_value(self, info: dict) -> object:
        type_repr = (info.get("type_repr") or "").lower()
        data_type = (info.get("data_type") or "").lower()
        udt_name = (info.get("udt_name") or "").lower()

        if "vector" in type_repr or udt_name == "vector":
            dim = self._parse_vector_dimension(type_repr)
            if dim:
                zeros = ",".join("0" for _ in range(dim))
                return f"[{zeros}]"
            return "[]"

        if data_type == "array":
            return []
        if data_type in {"json", "jsonb"}:
            return json.dumps([])
        return ""

    def _parse_vector_dimension(self, type_repr: str) -> int | None:
        if not type_repr:
            return None
        if "vector(" not in type_repr:
            return None
        try:
            start = type_repr.index("vector(") + len("vector(")
            end = type_repr.index(")", start)
            return int(type_repr[start:end])
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Ingestion run reports
    # ------------------------------------------------------------------ #
    def create_ingestion_run_report(
        self,
        *,
        run_id: str,
        bucket: str,
        company: str | None,
        project: str | None,
        project_id: str | None,
        module: int | None,
        mode: str | None,
        core_check: str | None,
        md_suffix: str | None,
        force_core: bool,
        force_tox: bool,
        started_at: str | None,
        ended_at: str | None,
        doc_processed: int,
        doc_failed: int,
        doc_skipped: int,
        core_runs: int,
        core_failed: int,
        tox_runs: int,
        tox_failed: int,
        error_summary: dict,
        report_json: dict,
    ) -> str | None:
        db = self.session
        row = db.execute(
            sqltext(
                """
                INSERT INTO ncd_ingestion_run_reports (
                    run_id, bucket, company, project, project_id, module, mode,
                    core_check, md_suffix, force_core, force_tox,
                    started_at, ended_at,
                    doc_processed, doc_failed, doc_skipped,
                    core_runs, core_failed, tox_runs, tox_failed,
                    error_summary, report_json
                )
                VALUES (
                    :run_id, :bucket, :company, :project, :project_id, :module, :mode,
                    :core_check, :md_suffix, :force_core, :force_tox,
                    :started_at, :ended_at,
                    :doc_processed, :doc_failed, :doc_skipped,
                    :core_runs, :core_failed, :tox_runs, :tox_failed,
                    CAST(:error_summary AS jsonb), CAST(:report_json AS jsonb)
                )
                ON CONFLICT (run_id) DO UPDATE SET
                    ended_at = EXCLUDED.ended_at,
                    doc_processed = EXCLUDED.doc_processed,
                    doc_failed = EXCLUDED.doc_failed,
                    doc_skipped = EXCLUDED.doc_skipped,
                    core_runs = EXCLUDED.core_runs,
                    core_failed = EXCLUDED.core_failed,
                    tox_runs = EXCLUDED.tox_runs,
                    tox_failed = EXCLUDED.tox_failed,
                    error_summary = EXCLUDED.error_summary,
                    report_json = EXCLUDED.report_json
                RETURNING id
                """
            ),
            {
                "run_id": run_id,
                "bucket": bucket,
                "company": company,
                "project": project,
                "project_id": project_id,
                "module": module,
                "mode": mode,
                "core_check": core_check,
                "md_suffix": md_suffix,
                "force_core": force_core,
                "force_tox": force_tox,
                "started_at": started_at,
                "ended_at": ended_at,
                "doc_processed": doc_processed,
                "doc_failed": doc_failed,
                "doc_skipped": doc_skipped,
                "core_runs": core_runs,
                "core_failed": core_failed,
                "tox_runs": tox_runs,
                "tox_failed": tox_failed,
                "error_summary": json.dumps(error_summary),
                "report_json": json.dumps(report_json),
            },
        ).scalar()
        db.commit()
        return str(row) if row else None

    def fetch_document_id_for_version(self, document_version_id: str) -> str | None:
        db = self.session
        row = db.execute(
            sqltext(
                """
                SELECT document_id
                FROM document_versions
                WHERE id = :dvid
                """
            ),
            {"dvid": document_version_id},
        ).scalar()
        return str(row) if row else None

    def upsert_ingestion_status(
        self,
        *,
        s3_bucket: str,
        s3_key: str,
        s3_version_id: str | None,
        content_hash: str,
        status: str,
        document_version_id: str | None = None,
        error_message: str | None = None,
    ) -> dict:
        """
        Upsert ingestion status row.
        """
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                INSERT INTO document_ingestion_status (
                    s3_bucket, s3_key, s3_version_id,
                    content_hash, status, document_version_id, error_message
                )
                VALUES (:bucket, :key, :version_id, :hash, :status, :dvid, :err)
                ON CONFLICT (s3_bucket, s3_key, content_hash)
                DO UPDATE SET
                    status = EXCLUDED.status,
                    document_version_id = COALESCE(EXCLUDED.document_version_id, document_ingestion_status.document_version_id),
                    error_message = EXCLUDED.error_message,
                    updated_at = now()
                RETURNING id, status, document_version_id
                """
                ),
                {
                    "bucket": s3_bucket,
                    "key": s3_key,
                    "version_id": s3_version_id,
                    "hash": content_hash,
                    "status": status,
                    "dvid": document_version_id,
                    "err": error_message,
                },
            )
            .mappings()
            .first()
        )
        db.commit()
        return dict(row) if row else {}

    def fetch_ingestion_status(
        self, *, s3_bucket: str, s3_key: str, content_hash: str
    ) -> dict | None:
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                SELECT id, status, document_version_id
                FROM document_ingestion_status
                WHERE s3_bucket = :bucket AND s3_key = :key AND content_hash = :hash
                """
                ),
                {"bucket": s3_bucket, "key": s3_key, "hash": content_hash},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def fetch_ingestion_status_for_key(
        self, *, s3_bucket: str, s3_key: str, s3_version_id: str | None = None
    ) -> dict | None:
        """
        Fetch the most recent ingestion status for a given S3 key.
        Optionally scope the lookup to a specific S3 version id.
        """
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                SELECT id, status, document_version_id, content_hash, s3_version_id, updated_at, error_message
                FROM document_ingestion_status
                WHERE s3_bucket = :bucket
                  AND s3_key = :key
                  AND (:version_id IS NULL OR s3_version_id = :version_id)
                ORDER BY updated_at DESC
                LIMIT 1
                """
                ),
                {
                    "bucket": s3_bucket,
                    "key": s3_key,
                    "version_id": s3_version_id,
                },
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def upsert_pipeline_status(
        self,
        *,
        s3_bucket: str,
        s3_key: str,
        s3_version_id: str | None,
        content_hash: str,
        pipeline: str,
        status: str,
        document_version_id: str | None = None,
        source_document_id: str | None = None,
        study_id: str | None = None,
        error_message: str | None = None,
    ) -> dict:
        """
        Upsert per-pipeline ingestion status.
        """
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                INSERT INTO ncd_ingestion_pipeline_status (
                    s3_bucket, s3_key, s3_version_id,
                    content_hash, pipeline, status,
                    document_version_id, source_document_id, study_id,
                    error_message
                )
                VALUES (
                    :bucket, :key, :version_id,
                    :hash, :pipeline, :status,
                    :dvid, :sdid, :study_id,
                    :err
                )
                ON CONFLICT (s3_bucket, s3_key, content_hash, pipeline)
                DO UPDATE SET
                    status = EXCLUDED.status,
                    document_version_id = COALESCE(EXCLUDED.document_version_id, ncd_ingestion_pipeline_status.document_version_id),
                    source_document_id = COALESCE(EXCLUDED.source_document_id, ncd_ingestion_pipeline_status.source_document_id),
                    study_id = COALESCE(EXCLUDED.study_id, ncd_ingestion_pipeline_status.study_id),
                    error_message = EXCLUDED.error_message,
                    updated_at = now()
                RETURNING id, status, document_version_id, source_document_id, study_id
                """
                ),
                {
                    "bucket": s3_bucket,
                    "key": s3_key,
                    "version_id": s3_version_id,
                    "hash": content_hash,
                    "pipeline": pipeline,
                    "status": status,
                    "dvid": document_version_id,
                    "sdid": source_document_id,
                    "study_id": study_id,
                    "err": error_message,
                },
            )
            .mappings()
            .first()
        )
        db.commit()
        return dict(row) if row else {}

    def fetch_pipeline_status(
        self,
        *,
        s3_bucket: str,
        s3_key: str,
        content_hash: str,
        pipeline: str,
    ) -> dict | None:
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                SELECT id, status, document_version_id, source_document_id, study_id, error_message
                FROM ncd_ingestion_pipeline_status
                WHERE s3_bucket = :bucket
                  AND s3_key = :key
                  AND content_hash = :hash
                  AND pipeline = :pipeline
                """
                ),
                {
                    "bucket": s3_bucket,
                    "key": s3_key,
                    "hash": content_hash,
                    "pipeline": pipeline,
                },
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def fetch_pipeline_status_for_key(
        self,
        *,
        s3_bucket: str,
        s3_key: str,
        pipeline: str,
        s3_version_id: str | None = None,
    ) -> dict | None:
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                SELECT id, status, document_version_id, source_document_id, study_id,
                       content_hash, s3_version_id, updated_at, error_message
                FROM ncd_ingestion_pipeline_status
                WHERE s3_bucket = :bucket
                  AND s3_key = :key
                  AND pipeline = :pipeline
                  AND (:version_id IS NULL OR s3_version_id = :version_id)
                ORDER BY updated_at DESC
                LIMIT 1
                """
                ),
                {
                    "bucket": s3_bucket,
                    "key": s3_key,
                    "pipeline": pipeline,
                    "version_id": s3_version_id,
                },
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def upsert_source_document(
        self,
        *,
        project_id: str,
        file_name: str,
        module: str,
        sha256: str,
        ctd_section: str | None = None,
    ) -> str:
        """
        Create or reuse a source document row based on project + sha256 hash.
        Returns the source_document_id.
        """
        db = self.session
        existing = db.execute(
            sqltext(
                """
                SELECT id FROM ncd_source_document
                WHERE project_id = :pid AND sha256 = :sha
                """
            ),
            {"pid": project_id, "sha": sha256},
        ).scalar()

        if existing:
            return str(existing)

        new_id = db.execute(
            sqltext(
                """
                INSERT INTO ncd_source_document (project_id, file_name, module, ctd_section, sha256)
                VALUES (:pid, :fname, :module, :ctd, :sha)
                RETURNING id;
                """
            ),
            {
                "pid": project_id,
                "fname": file_name,
                "module": module,
                "ctd": ctd_section,
                "sha": sha256,
            },
        ).scalar()
        if new_id is None:
            raise RuntimeError("Failed to insert ncd_source_document")
        db.commit()
        return str(new_id)

    def replace_document_pages(
        self, *, source_document_id: str, pages: Iterable[PageText]
    ) -> None:
        """
        Replace all page records for a source document.
        """
        db = self.session
        db.execute(
            sqltext(
                """
                DELETE FROM ncd_document_page
                WHERE source_document_id = :sid
                """
            ),
            {"sid": source_document_id},
        )
        for page in pages:
            db.execute(
                sqltext(
                    """
                    INSERT INTO ncd_document_page (source_document_id, page_number, text)
                    VALUES (:sid, :pnum, :txt)
                    """
                ),
                {"sid": source_document_id, "pnum": page.page_number, "txt": page.text},
            )
        db.commit()

    # ------------------------------------------------------------------ #
    # Chunks + embeddings
    # ------------------------------------------------------------------ #
    def insert_text_chunks(self, chunks: List[TextChunkRecord]) -> List[str]:
        """
        Insert text chunks and optional embeddings.
        Returns chunk IDs in order.
        """
        if not chunks:
            return []
        db = self.session
        chunk_ids: List[str] = []
        for chunk in chunks:
            cid = db.execute(
                sqltext(
                    """
                    INSERT INTO ncd_text_chunk (
                        source_document_id, study_id, section_label,
                        page_from, page_to, offset_start, offset_end,
                        raw_text
                    ) VALUES (
                        :sid, :study_id, :section_label,
                        :page_from, :page_to, :offset_start, :offset_end,
                        :raw_text
                    )
                    RETURNING id
                    """
                ),
                {
                    "sid": chunk.source_document_id,
                    "study_id": chunk.study_id,
                    "section_label": chunk.section_label,
                    "page_from": chunk.page_from,
                    "page_to": chunk.page_to,
                    "offset_start": chunk.offset_start,
                    "offset_end": chunk.offset_end,
                    "raw_text": chunk.raw_text,
                },
            ).scalar()
            if cid is None:
                raise RuntimeError("Failed to insert ncd_text_chunk")
            chunk_ids.append(str(cid))

            if chunk.embedding is not None:
                db.execute(
                    sqltext(
                        """
                        INSERT INTO ncd_text_chunk_embedding (chunk_id, embedding)
                        VALUES (:cid, :emb)
                        ON CONFLICT (chunk_id) DO UPDATE SET embedding = EXCLUDED.embedding
                        """
                    ),
                    {"cid": cid, "emb": list(chunk.embedding)},
                )
        db.commit()

    # ------------------------------------------------------------------ #
    # Extraction + NCD persistence helpers
    # ------------------------------------------------------------------ #

    def upsert_document_assets(
        self, assets: Sequence[DocumentAssetRecord]
    ) -> List[dict]:
        if not assets:
            return []
        db = self.session
        rows: List[dict] = []
        for asset in assets:
            row = (
                db.execute(
                    sqltext(
                        """
                    INSERT INTO document_assets (
                        document_version_id, asset_type,
                        page_number, index_on_page,
                        s3_bucket, s3_key,
                        caption, description, keywords, extra_attributes
                    ) VALUES (
                        :dvid, :atype,
                        :page_number, :index_on_page,
                        :bucket, :key,
                        :caption, :description, :keywords, CAST(:extra AS jsonb)
                    )
                    ON CONFLICT (document_version_id, asset_type, page_number, index_on_page, s3_key)
                    DO UPDATE SET
                        caption = EXCLUDED.caption,
                        description = EXCLUDED.description,
                        keywords = EXCLUDED.keywords,
                        extra_attributes = EXCLUDED.extra_attributes
                    RETURNING id, page_number, asset_type
                    """
                    ),
                    {
                        "dvid": asset.document_version_id,
                        "atype": asset.asset_type,
                        "page_number": asset.page_number,
                        "index_on_page": asset.index_on_page,
                        "bucket": asset.s3_bucket,
                        "key": asset.s3_key,
                        "caption": asset.caption,
                        "description": asset.description,
                        "keywords": list(asset.keywords or []),
                        "extra": json.dumps(
                            asset.extra_attributes or {}, ensure_ascii=True
                        ),
                    },
                )
                .mappings()
                .first()
            )
            if row:
                rows.append(dict(row))
        db.commit()
        return rows

    def replace_document_key_sections(
        self, sections: Sequence[DocumentKeySectionRecord]
    ) -> int:
        if not sections:
            return 0
        db = self.session
        document_version_id = sections[0].document_version_id
        db.execute(
            sqltext(
                """
                DELETE FROM document_key_sections
                WHERE document_version_id = :dvid
                """
            ),
            {"dvid": document_version_id},
        )
        for section in sections:
            db.execute(
                sqltext(
                    """
                    INSERT INTO document_key_sections (
                        document_version_id, section_type, text,
                        page_start, page_end, char_start, char_end,
                        asset_ids, model_name, confidence
                    ) VALUES (
                        :dvid, :stype, :text,
                        :page_start, :page_end, :char_start, :char_end,
                        CAST(:asset_ids AS uuid[]), :model_name, :confidence
                    )
                    """
                ),
                {
                    "dvid": section.document_version_id,
                    "stype": section.section_type,
                    "text": section.text,
                    "page_start": section.page_start,
                    "page_end": section.page_end,
                    "char_start": section.char_start,
                    "char_end": section.char_end,
                    "asset_ids": list(section.asset_ids or []),
                    "model_name": section.model_name,
                    "confidence": section.confidence,
                },
            )
        db.commit()
        return len(sections)

    def fetch_ctd_section_reference(
        self,
        *,
        tenant_id: str,
        project_id: str,
        bucket: str,
        element_number: str,
    ) -> Optional[dict]:
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                    SELECT id, tenant_id, project_id, bucket, element_number,
                           section_number, template_payload, module4_sections,
                           payload, created_at, updated_at
                    FROM ncd_ctd_section_reference
                    WHERE tenant_id = :tenant_id
                      AND project_id = :project_id
                      AND bucket = :bucket
                      AND element_number = :element_number
                    LIMIT 1
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "bucket": bucket,
                    "element_number": element_number,
                },
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def upsert_ctd_section_reference(
        self,
        *,
        record: CTDSectionReferenceRecord,
    ) -> str:
        db = self.session
        template_payload = record.template_payload or {}
        payload = record.payload or {}
        module4_sections = list(record.module4_sections or [])
        ref_id = db.execute(
            sqltext(
                """
                INSERT INTO ncd_ctd_section_reference (
                    tenant_id, project_id, bucket, element_number,
                    section_number, template_payload, module4_sections,
                    payload
                ) VALUES (
                    :tenant_id, :project_id, :bucket, :element_number,
                    :section_number, CAST(:template_payload AS jsonb),
                    CAST(:module4_sections AS text[]), CAST(:payload AS jsonb)
                )
                ON CONFLICT (tenant_id, project_id, bucket, element_number)
                DO UPDATE SET
                    section_number = EXCLUDED.section_number,
                    template_payload = EXCLUDED.template_payload,
                    module4_sections = EXCLUDED.module4_sections,
                    payload = EXCLUDED.payload,
                    updated_at = now()
                RETURNING id
                """
            ),
            {
                "tenant_id": record.tenant_id,
                "project_id": record.project_id,
                "bucket": record.bucket,
                "element_number": record.element_number,
                "section_number": record.section_number,
                "template_payload": json.dumps(template_payload, ensure_ascii=True),
                "module4_sections": module4_sections,
                "payload": json.dumps(payload, ensure_ascii=True),
            },
        ).scalar()
        if ref_id is None:
            raise RuntimeError("Failed to upsert ncd_ctd_section_reference")
        db.commit()
        return str(ref_id)

    def fetch_ctd_section_summary(
        self,
        *,
        summary_id: str,
    ) -> Optional[dict]:
        if not self._table_exists("ncd_ctd_section_summary"):
            return None
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                    SELECT id, tenant_id, project_id, bucket, section_number,
                           element_numbers, summary_text, final_text,
                           status, user_prompt, user_comment, previous_id,
                           model_name, embedding, created_at, updated_at
                    FROM ncd_ctd_section_summary
                    WHERE id = :summary_id
                    LIMIT 1
                    """
                ),
                {"summary_id": summary_id},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def fetch_latest_ctd_section_summary(
        self,
        *,
        tenant_id: str,
        project_id: str,
        bucket: str,
        section_number: str,
        status: Optional[str] = None,
    ) -> Optional[dict]:
        if not self._table_exists("ncd_ctd_section_summary"):
            return None
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                    SELECT id, tenant_id, project_id, bucket, section_number,
                           element_numbers, summary_text, final_text,
                           status, user_prompt, user_comment, previous_id,
                           model_name, embedding, created_at, updated_at
                    FROM ncd_ctd_section_summary
                    WHERE tenant_id = :tenant_id
                      AND project_id = :project_id
                      AND bucket = :bucket
                      AND section_number = :section_number
                      AND (:status IS NULL OR status = :status)
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "bucket": bucket,
                    "section_number": section_number,
                    "status": status,
                },
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def insert_ctd_section_summary(
        self,
        *,
        record: CTDSectionSummaryRecord,
    ) -> str:
        if not self._table_exists("ncd_ctd_section_summary"):
            raise RuntimeError("ncd_ctd_section_summary table is missing")
        db = self.session
        embedding_literal = (
            self._vector_literal(record.embedding) if record.embedding else None
        )
        summary_id = db.execute(
            sqltext(
                """
                INSERT INTO ncd_ctd_section_summary (
                    tenant_id, project_id, bucket, section_number,
                    element_numbers, summary_text, final_text,
                    status, user_prompt, user_comment, previous_id,
                    model_name, embedding
                ) VALUES (
                    :tenant_id, :project_id, :bucket, :section_number,
                    CAST(:element_numbers AS text[]), :summary_text, :final_text,
                    :status, :user_prompt, :user_comment, :previous_id,
                    :model_name, CAST(:embedding AS vector)
                )
                RETURNING id
                """
            ),
            {
                "tenant_id": record.tenant_id,
                "project_id": record.project_id,
                "bucket": record.bucket,
                "section_number": record.section_number,
                "element_numbers": list(record.element_numbers or []),
                "summary_text": record.summary_text,
                "final_text": None,
                "status": record.status,
                "user_prompt": record.user_prompt,
                "user_comment": record.user_comment,
                "previous_id": record.previous_id,
                "model_name": record.model_name,
                "embedding": embedding_literal,
            },
        ).scalar()
        if summary_id is None:
            raise RuntimeError("Failed to insert ncd_ctd_section_summary")
        db.commit()
        return str(summary_id)

    def approve_ctd_section_summary(
        self,
        *,
        summary_id: str,
        final_text: str,
        model_name: Optional[str] = None,
    ) -> Optional[dict]:
        if not self._table_exists("ncd_ctd_section_summary"):
            return None
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                    UPDATE ncd_ctd_section_summary
                    SET final_text = :final_text,
                        status = 'approved',
                        model_name = COALESCE(:model_name, model_name),
                        updated_at = now()
                    WHERE id = :summary_id
                    RETURNING id, tenant_id, project_id, bucket, section_number,
                              element_numbers, summary_text, final_text,
                              status, user_prompt, user_comment, previous_id,
                              model_name, embedding, created_at, updated_at
                    """
                ),
                {
                    "summary_id": summary_id,
                    "final_text": final_text,
                    "model_name": model_name,
                },
            )
            .mappings()
            .first()
        )
        if row:
            db.commit()
            return dict(row)
        db.commit()
        return None

    def fetch_ctd_tabulated_summary(
        self,
        *,
        summary_id: str,
    ) -> Optional[dict]:
        if not self._table_exists("ncd_ctd_tabulated_summary"):
            return None
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                    SELECT id, tenant_id, project_id, bucket, section_number,
                           table_payload, final_payload, status, user_prompt,
                           user_comment, previous_id, model_name, embedding,
                           created_at, updated_at
                    FROM ncd_ctd_tabulated_summary
                    WHERE id = :summary_id
                    LIMIT 1
                    """
                ),
                {"summary_id": summary_id},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def fetch_latest_ctd_tabulated_summary(
        self,
        *,
        tenant_id: str,
        project_id: str,
        bucket: str,
        section_number: str,
        status: Optional[str] = None,
    ) -> Optional[dict]:
        if not self._table_exists("ncd_ctd_tabulated_summary"):
            return None
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                    SELECT id, tenant_id, project_id, bucket, section_number,
                           table_payload, final_payload, status, user_prompt,
                           user_comment, previous_id, model_name, embedding,
                           created_at, updated_at
                    FROM ncd_ctd_tabulated_summary
                    WHERE tenant_id = :tenant_id
                      AND project_id = :project_id
                      AND bucket = :bucket
                      AND section_number = :section_number
                      AND (:status IS NULL OR status = :status)
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "bucket": bucket,
                    "section_number": section_number,
                    "status": status,
                },
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def insert_ctd_tabulated_summary(
        self,
        *,
        record: CTDTabulatedSummaryRecord,
    ) -> str:
        if not self._table_exists("ncd_ctd_tabulated_summary"):
            raise RuntimeError("ncd_ctd_tabulated_summary table is missing")
        db = self.session
        embedding_literal = (
            self._vector_literal(record.embedding) if record.embedding else None
        )
        summary_id = db.execute(
            sqltext(
                """
                INSERT INTO ncd_ctd_tabulated_summary (
                    tenant_id, project_id, bucket, section_number,
                    table_payload, final_payload, status, user_prompt,
                    user_comment, previous_id, model_name, embedding
                ) VALUES (
                    :tenant_id, :project_id, :bucket, :section_number,
                    CAST(:table_payload AS jsonb), CAST(:final_payload AS jsonb),
                    :status, :user_prompt, :user_comment, :previous_id,
                    :model_name, CAST(:embedding AS vector)
                )
                RETURNING id
                """
            ),
            {
                "tenant_id": record.tenant_id,
                "project_id": record.project_id,
                "bucket": record.bucket,
                "section_number": record.section_number,
                "table_payload": json.dumps(record.table_payload, ensure_ascii=False),
                "final_payload": None,
                "status": record.status,
                "user_prompt": record.user_prompt,
                "user_comment": record.user_comment,
                "previous_id": record.previous_id,
                "model_name": record.model_name,
                "embedding": embedding_literal,
            },
        ).scalar()
        if summary_id is None:
            raise RuntimeError("Failed to insert ncd_ctd_tabulated_summary")
        db.commit()
        return str(summary_id)

    def approve_ctd_tabulated_summary(
        self,
        *,
        summary_id: str,
        final_payload: dict,
        model_name: Optional[str] = None,
    ) -> Optional[dict]:
        if not self._table_exists("ncd_ctd_tabulated_summary"):
            return None
        db = self.session
        row = (
            db.execute(
                sqltext(
                    """
                    UPDATE ncd_ctd_tabulated_summary
                    SET final_payload = CAST(:final_payload AS jsonb),
                        status = 'approved',
                        model_name = COALESCE(:model_name, model_name),
                        updated_at = now()
                    WHERE id = :summary_id
                    RETURNING id, tenant_id, project_id, bucket, section_number,
                              table_payload, final_payload, status, user_prompt,
                              user_comment, previous_id, model_name, embedding,
                              created_at, updated_at
                    """
                ),
                {
                    "summary_id": summary_id,
                    "final_payload": json.dumps(final_payload, ensure_ascii=False),
                    "model_name": model_name,
                },
            )
            .mappings()
            .first()
        )
        if row:
            db.commit()
            return dict(row)
        db.commit()
        return None

    def fetch_asset_ids_for_pages(
        self,
        *,
        document_version_id: str,
        page_start: int | None,
        page_end: int | None,
    ) -> List[str]:
        if page_start is None or page_end is None:
            return []
        db = self.session
        rows = (
            db.execute(
                sqltext(
                    """
                SELECT id
                FROM document_assets
                WHERE document_version_id = :dvid
                  AND page_number BETWEEN :pstart AND :pend
                """
                ),
                {"dvid": document_version_id, "pstart": page_start, "pend": page_end},
            )
            .scalars()
            .all()
        )
        return [str(row) for row in rows]

    def fetch_document_version_id_for_study(self, *, study_id: str) -> str | None:
        db = self.session
        row = db.execute(
            sqltext(
                """
                SELECT dv.id
                FROM ncd_study s
                JOIN ncd_source_document sd ON s.main_source_document_id = sd.id
                JOIN document_versions dv ON dv.content_hash = sd.sha256
                WHERE s.id = :sid
                LIMIT 1
                """
            ),
            {"sid": study_id},
        ).scalar()
        return str(row) if row else None

    def create_extraction_run(
        self,
        *,
        document_version_id: str,
        module: str = "4",
        extractor: str,
        model: str,
        status: str = "completed",
        created_by: str | None = None,
    ) -> str:
        db = self.session
        run_id = db.execute(
            sqltext(
                """
                INSERT INTO extraction_runs (
                    document_version_id, module, extractor, model, status, created_by
                )
                VALUES (:dvid, :module, :ext, :model, :status, :created_by)
                RETURNING id
                """
            ),
            {
                "dvid": document_version_id,
                "module": module,
                "ext": extractor,
                "model": model,
                "status": status,
                "created_by": created_by,
            },
        ).scalar()
        if run_id is None:
            raise RuntimeError("Failed to insert extraction_run")
        db.commit()
        return str(run_id)

    def upsert_study(
        self,
        *,
        study_id: str,
        study_type: str,
        source_document_id: str,
        species: str | None = None,
        route: str | None = None,
        duration: str | None = None,
        project_id: str | None = None,
    ) -> str:
        """
        Insert a study if missing; return its UUID.
        """
        db = self.session
        if self._table_exists("ncd_studies"):
            inserted = db.execute(
                sqltext(
                    """
                    INSERT INTO ncd_studies (
                        study_id, study_type, species, route, duration, source_document_id
                    )
                    VALUES (:sid, :stype, :species, :route, :duration, :src_doc)
                    ON CONFLICT (study_id) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "sid": study_id,
                    "stype": study_type,
                    "species": species,
                    "route": route,
                    "duration": duration,
                    "src_doc": source_document_id,
                },
            ).scalar()
            if inserted:
                db.commit()
                return str(inserted)

            existing = db.execute(
                sqltext(
                    """
                    SELECT id FROM ncd_studies WHERE study_id = :sid
                    """
                ),
                {"sid": study_id},
            ).scalar()
            if existing is None:
                raise RuntimeError("Failed to upsert study")
            return str(existing)

        if not self._table_exists("ncd_study"):
            raise RuntimeError("Missing ncd_study/ncd_studies table for study upsert")

        if project_id is None and source_document_id:
            project_id = db.execute(
                sqltext(
                    """
                    SELECT project_id
                    FROM ncd_source_document
                    WHERE id = :sid
                    """
                ),
                {"sid": source_document_id},
            ).scalar()

        duration_days = self._parse_duration_days(duration)
        extra_attributes = {}
        if duration and duration_days is None:
            extra_attributes["duration_raw"] = duration
        if source_document_id:
            extra_attributes["legacy_document_id"] = source_document_id

        existing = db.execute(
            sqltext(
                """
                SELECT id
                FROM ncd_study
                WHERE sponsor_study_id = :sid
                LIMIT 1
                """
            ),
            {"sid": study_id},
        ).scalar()
        if existing:
            if project_id:
                db.execute(
                    sqltext(
                        """
                        UPDATE ncd_study
                        SET project_id = COALESCE(project_id, :pid)
                        WHERE id = :id
                        """
                    ),
                    {"pid": project_id, "id": existing},
                )
                db.commit()
            return str(existing)

        columns = [
            "sponsor_study_id",
            "study_type",
            "species",
            "route",
            "duration_days",
            "extra_attributes",
        ]
        values: Dict[str, Any] = {
            "sid": study_id,
            "stype": study_type,
            "species": species,
            "route": route,
            "duration_days": duration_days,
            "extra": json.dumps(extra_attributes) if extra_attributes else "{}",
        }
        if project_id:
            columns.insert(0, "project_id")
            values["project_id"] = project_id

        cols_sql = ", ".join(columns)
        params_sql = ", ".join(
            f":{param}"
            for param in (
                "project_id" if project_id else None,
                "sid",
                "stype",
                "species",
                "route",
                "duration_days",
                "extra",
            )
            if param
        )
        insert_sql = f"""
                INSERT INTO ncd_study (
                    {cols_sql}
                )
                VALUES (
                    {params_sql}
                )
                RETURNING id
                """
        inserted = db.execute(sqltext(insert_sql), values).scalar()
        if inserted is None:
            raise RuntimeError("Failed to upsert study")
        db.commit()
        return str(inserted)

    def insert_noael(
        self,
        *,
        study_id: str,
        dose: float | None,
        dose_unit: str | None,
        species: str | None,
        sex: str | None,
        endpoint: str | None,
        value: str | None,
    ) -> str:
        db = self.session
        if not self._table_exists("ncd_noael"):
            raise RuntimeError(
                "Missing ncd_noael table; run legacy NCD schema for LangChain NOAEL extraction."
            )
        noael_id = db.execute(
            sqltext(
                """
                INSERT INTO ncd_noael (
                    study_id, dose, dose_unit, species, sex, endpoint, value
                )
                VALUES (:study_id, :dose, :dose_unit, :species, :sex, :endpoint, :value)
                RETURNING id
                """
            ),
            {
                "study_id": study_id,
                "dose": dose,
                "dose_unit": dose_unit,
                "species": species,
                "sex": sex,
                "endpoint": endpoint,
                "value": value,
            },
        ).scalar()
        if noael_id is None:
            raise RuntimeError("Failed to insert ncd_noael")
        db.commit()
        return str(noael_id)

    def insert_pk_parameter(
        self,
        *,
        study_id: str,
        parameter: str,
        value: float | None,
        unit: str | None,
        dose_group: str | None,
    ) -> str:
        db = self.session
        if not self._table_exists("ncd_pk_parameters"):
            raise RuntimeError(
                "Missing ncd_pk_parameters table; run legacy NCD schema for LangChain PK extraction."
            )
        pk_id = db.execute(
            sqltext(
                """
                INSERT INTO ncd_pk_parameters (
                    study_id, parameter, value, unit, dose_group
                )
                VALUES (:study_id, :parameter, :value, :unit, :dose_group)
                RETURNING id
                """
            ),
            {
                "study_id": study_id,
                "parameter": parameter,
                "value": value,
                "unit": unit,
                "dose_group": dose_group,
            },
        ).scalar()
        if pk_id is None:
            raise RuntimeError("Failed to insert ncd_pk_parameters")
        db.commit()
        return str(pk_id)

    def insert_extracted_entity(
        self,
        *,
        extraction_run_id: str,
        document_version_id: str,
        entity_type: str,
        entity_id: str,
        anchor: dict,
        confidence: float | None,
    ) -> str:
        db = self.session
        anchor_json = json.dumps(anchor) if anchor is not None else None
        ent_id = db.execute(
            sqltext(
                """
                INSERT INTO extracted_entities (
                    extraction_run_id, document_version_id,
                    entity_type, entity_id, anchor, confidence
                )
                VALUES (:run_id, :doc_version_id, :etype, :eid, CAST(:anchor AS jsonb), :conf)
                ON CONFLICT (entity_type, entity_id)
                DO UPDATE SET
                    anchor = EXCLUDED.anchor,
                    confidence = EXCLUDED.confidence,
                    extraction_run_id = EXCLUDED.extraction_run_id,
                    document_version_id = EXCLUDED.document_version_id
                RETURNING id
                """
            ),
            {
                "run_id": extraction_run_id,
                "doc_version_id": document_version_id,
                "etype": entity_type,
                "eid": entity_id,
                "anchor": anchor_json,
                "conf": confidence,
            },
        ).scalar()
        if ent_id is None:
            raise RuntimeError("Failed to insert extracted_entities")
        db.commit()
        return str(ent_id)

    def create_low_confidence_comment(
        self,
        *,
        document_version_id: str,
        anchor: dict,
        content: str,
        created_by: str | None = None,
    ) -> str:
        db = self.session
        anchor_json = json.dumps(anchor) if anchor is not None else None
        comment_id = db.execute(
            sqltext(
                """
                INSERT INTO document_comments (
                    document_version_id, parent_id, status,
                    anchor, content, created_by
                )
                VALUES (:doc_version_id, NULL, 'open', CAST(:anchor AS jsonb), :content, :created_by)
                RETURNING id
                """
            ),
            {
                "doc_version_id": document_version_id,
                "anchor": anchor_json,
                "content": content,
                "created_by": created_by,
            },
        ).scalar()
        if comment_id is None:
            raise RuntimeError("Failed to insert document_comment")
        db.commit()
        return str(comment_id)
        return chunk_ids


__all__ = [
    "NCDRepository",
    "PageText",
    "TextChunkRecord",
]
