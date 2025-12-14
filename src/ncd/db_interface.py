"""
Lightweight database interface for persisting Module 4 extraction artifacts into the NCD schema.

This wrapper aligns the pipeline outputs (pages, chunks, embeddings) with the tables defined in
`ncd-schema.sql`. It keeps write operations small and composable so the PDF pipeline can
persist results step by step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

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
        return chunk_ids


__all__ = [
    "NCDRepository",
    "PageText",
    "TextChunkRecord",
]
