from __future__ import annotations

from typing import Dict

from ind_pipeline.stage_common import SimpleStageModule, StageModuleSpec


def _workload(payload: Dict[str, object]) -> Dict[str, object]:
    table_templates = payload.get("tables")
    table_count = len(table_templates) if isinstance(table_templates, list) else 0
    return {
        "notes": "Placeholder 2.6 tabulation stage.",
        "table_count": table_count,
    }


_MODULE = SimpleStageModule(
    StageModuleSpec(
        module_name="module26-tabulators",
        env_prefix="MODULE26_TABULATORS",
        description="Normalise and format data tables for Module 2.6 subsections.",
        default_queue_name="module26-tabulators-queue",
    ),
    work_fn=_workload,
)


def handle_message(payload: Dict[str, object]) -> Dict[str, object]:
    return _MODULE.handler(payload)


def run() -> None:
    _MODULE.run()


__all__ = ["handle_message", "run"]
