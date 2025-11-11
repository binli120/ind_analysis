"""Placeholder upload-ingestion module used for integration/infra scaffolding."""

from __future__ import annotations

from typing import Dict

from ind_pipeline.stage_common import SimpleStageModule, StageModuleSpec


def _workload(_: Dict[str, object]) -> Dict[str, object]:
    """Return a canned payload indicating ingestion succeeded."""
    return {
        "notes": "Simulated upload ingestion stage executed.",
    }


_MODULE = SimpleStageModule(
    StageModuleSpec(
        module_name="upload-ingestion",
        env_prefix="UPLOAD_INGESTION",
        description="Receive upload notifications, record metadata, and emit ingest completion events.",
        default_queue_name="upload-ingestion-queue",
    ),
    work_fn=_workload,
)


def handle_message(payload: Dict[str, object]) -> Dict[str, object]:
    """Expose the handler invoked by NotificationConsumer."""
    return _MODULE.handler(payload)


def run() -> None:
    """Start the module's consumer loop."""
    _MODULE.run()


__all__ = ["handle_message", "run"]
