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

from sqlalchemy import text as sqltext
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .db import SessionLocal, engine as default_engine


@dataclass
class PageText:
    page_number: int
    text: str


@dataclass
class TextChunkRecord:
    raw_text: str
    source_document_id: str
    page_from: Optional[int] = None
    page_to: Optional[int] = None
    offset_start: Optional[int] = None
    offset_end: Optional[int] = None
    section_label: Optional[str] = None
    study_id: Optional[str] = None
    embedding: Optional[Sequence[float]] = None


class NCDRepository:
    """Small helper for inserting document/pages/chunks into the NCD schema."""

    def __init__(self, engine: Engine | None = None, session: Session | None = None) -> None:
        self._engine = engine or default_engine
        self._external_session = session
        self._session: Session | None = None
        self._documents_has_content: bool | None = None
        self._documents_embedding_info: dict | None = None

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
        row = db.execute(
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
        ).mappings().first()
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
        row = db.execute(
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
        ).mappings().first()
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
        row = db.execute(
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
        ).mappings().first()
        db.commit()
        return dict(row) if row else {}

    def fetch_ingestion_status(
        self, *, s3_bucket: str, s3_key: str, content_hash: str
    ) -> dict | None:
        db = self.session
        row = db.execute(
            sqltext(
                """
                SELECT id, status, document_version_id
                FROM document_ingestion_status
                WHERE s3_bucket = :bucket AND s3_key = :key AND content_hash = :hash
                """
            ),
            {"bucket": s3_bucket, "key": s3_key, "hash": content_hash},
        ).mappings().first()
        return dict(row) if row else None

    def fetch_ingestion_status_for_key(
        self, *, s3_bucket: str, s3_key: str, s3_version_id: str | None = None
    ) -> dict | None:
        """
        Fetch the most recent ingestion status for a given S3 key.
        Optionally scope the lookup to a specific S3 version id.
        """
        db = self.session
        row = db.execute(
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
        ).mappings().first()
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
        row = db.execute(
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
        ).mappings().first()
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
        row = db.execute(
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
            {"bucket": s3_bucket, "key": s3_key, "hash": content_hash, "pipeline": pipeline},
        ).mappings().first()
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
        row = db.execute(
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
        ).mappings().first()
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
    ) -> str:
        """
        Insert a study if missing; return its UUID.
        """
        db = self.session
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
        ent_id = db.execute(
            sqltext(
                """
                INSERT INTO extracted_entities (
                    extraction_run_id, document_version_id,
                    entity_type, entity_id, anchor, confidence
                )
                VALUES (:run_id, :doc_version_id, :etype, :eid, :anchor, :conf)
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
                "anchor": anchor,
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
        comment_id = db.execute(
            sqltext(
                """
                INSERT INTO document_comments (
                    document_version_id, parent_id, status,
                    anchor, content, created_by
                )
                VALUES (:doc_version_id, NULL, 'open', :anchor, :content, :created_by)
                RETURNING id
                """
            ),
            {
                "doc_version_id": document_version_id,
                "anchor": anchor,
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
