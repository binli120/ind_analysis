from __future__ import annotations

from typing import Dict

from ind_pipeline.stage_common import SimpleStageModule, StageModuleSpec


def _workload(payload: Dict[str, object]) -> Dict[str, object]:
    has_narratives = bool(payload.get("narratives"))
    has_tables = bool(payload.get("tables"))
    return {
        "notes": "Placeholder Module 2.4 synthesis stage.",
        "narratives_present": has_narratives,
        "tables_present": has_tables,
    }


_MODULE = SimpleStageModule(
    StageModuleSpec(
        module_name="module24-synthesizer",
        env_prefix="MODULE24_SYNTHESIZER",
        description="Synthesize Module 2.4 integrated overview from Module 2.6 content.",
        default_queue_name="module24-synthesizer-queue",
    ),
    work_fn=_workload,
)


def handle_message(payload: Dict[str, object]) -> Dict[str, object]:
    return _MODULE.handler(payload)


def run() -> None:
    _MODULE.run()


__all__ = ["handle_message", "run"]
