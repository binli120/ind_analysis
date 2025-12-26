# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

import hashlib
from typing import cast

import fitz  # PyMuPDF
from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

"""
Coyright (c) Filynai.com 2024. All Rights Reserved.
author: Bin Lee
email: blee@filynai.com
PDF ingestion utilities for NCD document processing.
"""

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def create_source_document(
    db: Session, project_id: str, file_path: str, module: str
) -> str:
    file_name = file_path.split("/")[-1]
    file_hash = sha256_file(file_path)

    # If file already ingested for the same project, return its ID
    row = db.execute(
        sqltext("""
            SELECT id
            FROM ncd_source_document
            WHERE project_id = :pid
              AND sha256 = :sha
        """),
        {"pid": project_id, "sha": file_hash},
    ).scalar()

    if row is not None:
        return cast(str, row)

    new_id = db.execute(
        sqltext(
            """
            INSERT INTO ncd_source_document (project_id, file_name, module, sha256)
            VALUES (:pid, :fname, :module, :sha)
            RETURNING id
            """
        ),
        {"pid": project_id, "fname": file_name, "module": module, "sha": file_hash},
    ).scalar()

    if new_id is None:
        raise RuntimeError("Failed to create source document record")

    db.commit()
    return cast(str, new_id)


def extract_pages(db: Session, source_document_id: str, file_path: str) -> None:
    # Clear existing pages for reprocessing
    db.execute(
        sqltext("""
            DELETE FROM ncd_document_page
            WHERE source_document_id = :sid
        """),
        {"sid": source_document_id},
    )

    try:
        doc = fitz.open(file_path)
    except Exception as e:
        raise RuntimeError(f"Failed to read PDF: {e}")

    for i, page in enumerate(doc):
        text = page.get_text("text")

        db.execute(
            sqltext("""
                INSERT INTO ncd_document_page (
                    source_document_id, page_number, text
                ) VALUES (
                    :sid, :pnum, :txt
                )
            """),
            {"sid": source_document_id, "pnum": i + 1, "txt": text},
        )

    db.commit()
