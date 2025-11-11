"""Placeholder packaging/submission stage for pipeline scaffolding."""

from __future__ import annotations

from typing import Dict

from ind_pipeline.stage_common import SimpleStageModule, StageModuleSpec


def _workload(payload: Dict[str, object]) -> Dict[str, object]:
    """Report on the requested bundle to mimic packaging output."""
    return {
        "notes": "Placeholder packaging and submission stage.",
        "bundle_name": payload.get("bundle"),
    }


_MODULE = SimpleStageModule(
    StageModuleSpec(
        module_name="packaging-submission",
        env_prefix="PACKAGING_SUBMISSION",
        description="Assemble the eCTD structure, package, and prepare for ESG upload.",
        default_queue_name="packaging-submission-queue",
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
