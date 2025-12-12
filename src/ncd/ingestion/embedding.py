from typing import List

import numpy as np
from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session


def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    Replace this with your OpenAI embeddings:
    - Call embeddings API
    - Return list of vectors
    """
    # Temporary dummy embeddings
    dim = 1536
    return [list(np.random.rand(dim)) for _ in texts]


def embed_chunks(db: Session, chunk_ids: List[str]):
    if not chunk_ids:
        return

    rows = (
        db.execute(
            sqltext("""
            SELECT id, raw_text
            FROM ncd_text_chunk
            WHERE id = ANY(:ids)
        """),
            {"ids": chunk_ids},
        )
        .mappings()
        .all()
    )

    texts = [r["raw_text"] for r in rows]
    vectors = embed_texts(texts)

    for r, vec in zip(rows, vectors):
        db.execute(
            sqltext("""
                INSERT INTO ncd_text_chunk_embedding (chunk_id, embedding)
                VALUES (:cid, :emb)
                ON CONFLICT (chunk_id) DO UPDATE SET embedding = EXCLUDED.embedding;
            """),
            {"cid": r["id"], "emb": vec},
        )

    db.commit()
