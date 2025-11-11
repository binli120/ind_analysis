"""Placeholder stage that represents parsing/chunking of PDFs."""

from __future__ import annotations

from typing import Dict

from ind_pipeline.stage_common import SimpleStageModule, StageModuleSpec


def _workload(payload: Dict[str, object]) -> Dict[str, object]:
    """Report the count of chunks observed in the payload."""
    return {
        "notes": "Parsed PDF placeholder chunks.",
        "chunk_count": len(payload.get("chunks", [])) if isinstance(payload.get("chunks"), list) else 0,
    }


_MODULE = SimpleStageModule(
    StageModuleSpec(
        module_name="pdf-parsing-chunking",
        env_prefix="PDF_PARSING_CHUNKING",
        description="Parse documents, extract text/tables, and chunk by logical sections.",
        default_queue_name="pdf-parsing-chunking-queue",
    ),
    work_fn=_workload,
)


def handle_message(payload: Dict[str, object]) -> Dict[str, object]:
    """Expose the handler for NotificationConsumer wiring."""
    return _MODULE.handler(payload)


def run() -> None:
    """Start the module's consumer loop."""
    _MODULE.run()


__all__ = ["handle_message", "run"]
