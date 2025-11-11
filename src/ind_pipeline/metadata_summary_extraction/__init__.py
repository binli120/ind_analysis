"""Placeholder stage for metadata and summary extraction within the IND pipeline."""

from __future__ import annotations

from typing import Dict

from ind_pipeline.stage_common import SimpleStageModule, StageModuleSpec


def _workload(_: Dict[str, object]) -> Dict[str, object]:
    """Simulate metadata extraction output for testing/integration wiring."""
    return {
        "notes": "Simulated metadata and summary extraction stage.",
    }


_MODULE = SimpleStageModule(
    StageModuleSpec(
        module_name="metadata-summary-extraction",
        env_prefix="METADATA_SUMMARY_EXTRACTION",
        description="Extract key metadata and summaries including anchors into source text.",
        default_queue_name="metadata-summary-extraction-queue",
    ),
    work_fn=_workload,
)


def handle_message(payload: Dict[str, object]) -> Dict[str, object]:
    """Delegate to the common stage handler for incoming payloads."""
    return _MODULE.handler(payload)


def run() -> None:
    """Launch the shared NotificationConsumer loop for this stage."""
    _MODULE.run()


__all__ = ["handle_message", "run"]
