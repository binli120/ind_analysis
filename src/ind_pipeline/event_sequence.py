"""
Defines the canonical sequencing of pipeline stages. This is primarily used for
orchestration tooling and for informational purposes when wiring SNS topics.
"""

from __future__ import annotations

from typing import Dict, List

STAGE_SEQUENCE: List[str] = [
    "upload-ingestion",
    "pdf-parsing-chunking",
    "classification-template-matching",
    "metadata-summary-extraction",
    "embedding-indexing",
    "reranker",
    "module26-narrative-writers",
    "module26-tabulators",
    "module24-synthesizer",
    "validation-scoring",
    "packaging-submission",
]

# Some stages feed multiple downstream consumers (narratives and tabulators both
# feed into the Module 2.4 synthesizer). This successor map represents the
# intended fan-out, allowing infrastructure tooling to connect completion topics
# to the correct downstream inputs.
STAGE_SUCCESSORS: Dict[str, List[str]] = {
    "upload-ingestion": ["pdf-parsing-chunking"],
    "pdf-parsing-chunking": ["classification-template-matching"],
    "classification-template-matching": ["metadata-summary-extraction"],
    "metadata-summary-extraction": ["embedding-indexing"],
    "embedding-indexing": ["reranker"],
    "reranker": ["module26-narrative-writers", "module26-tabulators"],
    "module26-narrative-writers": ["module24-synthesizer"],
    "module26-tabulators": ["module24-synthesizer"],
    "module24-synthesizer": ["validation-scoring"],
    "validation-scoring": ["packaging-submission"],
    "packaging-submission": [],
}

__all__ = ["STAGE_SEQUENCE", "STAGE_SUCCESSORS"]
