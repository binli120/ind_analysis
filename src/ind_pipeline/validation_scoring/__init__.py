from __future__ import annotations

from typing import Dict

from ind_pipeline.stage_common import SimpleStageModule, StageModuleSpec


def _workload(payload: Dict[str, object]) -> Dict[str, object]:
    return {
        "notes": "Placeholder validation and scoring stage.",
        "checks_requested": payload.get("checks"),
    }


_MODULE = SimpleStageModule(
    StageModuleSpec(
        module_name="validation-scoring",
        env_prefix="VALIDATION_SCORING",
        description="Validate completeness, cross-references, and compute confidence scores.",
        default_queue_name="validation-scoring-queue",
    ),
    work_fn=_workload,
)


def handle_message(payload: Dict[str, object]) -> Dict[str, object]:
    return _MODULE.handler(payload)


def run() -> None:
    _MODULE.run()


__all__ = ["handle_message", "run"]
