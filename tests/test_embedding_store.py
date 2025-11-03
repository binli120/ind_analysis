from __future__ import annotations

import os

from pdf_analysis.service.embedding_store import SupabaseEmbeddingStore


def test_similarity_search_without_db_returns_empty(monkeypatch) -> None:
    # Ensure no DB connection or Supabase URL is available
    for key in [
        "SUPABASE_DB_URL",
        "SUPABASE_DB_CONNECTION",
        "SUPABASE_CONNECTION_STRING",
        "SUPABASE_URL",
        "SUPABASE_PROJECT_URL",
        "NEXT_PUBLIC_SUPABASE_URL",
    ]:
        monkeypatch.delenv(key, raising=False)

    store = SupabaseEmbeddingStore()

    results = store.search_similar_embedding([0.0, 0.0, 0.0])

    assert results == []
