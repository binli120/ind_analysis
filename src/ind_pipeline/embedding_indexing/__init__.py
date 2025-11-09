"""Placeholder stage for embedding generation and indexing."""

from __future__ import annotations

from typing import Dict

from ind_pipeline.stage_common import SimpleStageModule, StageModuleSpec


def _workload(payload: Dict[str, object]) -> Dict[str, object]:
    """Capture how many chunks would be embedded/indexed."""
    embedding_count = len(payload.get("chunks", [])) if isinstance(payload.get("chunks"), list) else 0
    return {
        "notes": "Placeholder embedding and indexing stage.",
        "embedding_count": embedding_count,
    }


_MODULE = SimpleStageModule(
    StageModuleSpec(
        module_name="embedding-indexing",
        env_prefix="EMBEDDING_INDEXING",
        description="Generate vector embeddings and store chunks in the search index.",
        default_queue_name="embedding-indexing-queue",
    ),
    work_fn=_workload,
)


def handle_message(payload: Dict[str, object]) -> Dict[str, object]:
    """Expose the handler expected by NotificationConsumer."""
    return _MODULE.handler(payload)


def run() -> None:
    """Start the module's NotificationConsumer loop."""
    _MODULE.run()


__all__ = ["handle_message", "run"]
