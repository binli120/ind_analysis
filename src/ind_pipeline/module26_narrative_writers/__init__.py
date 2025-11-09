from __future__ import annotations

from typing import Dict

from ind_pipeline.stage_common import SimpleStageModule, StageModuleSpec


def _workload(payload: Dict[str, object]) -> Dict[str, object]:
    sections = payload.get("sections")
    section_count = len(sections) if isinstance(sections, list) else 0
    return {
        "notes": "Placeholder 2.6 narrative drafting stage.",
        "section_count": section_count,
    }


_MODULE = SimpleStageModule(
    StageModuleSpec(
        module_name="module26-narrative-writers",
        env_prefix="MODULE26_NARRATIVE_WRITERS",
        description="Draft Module 2.6 narratives using curated evidence with citations.",
        default_queue_name="module26-narrative-writers-queue",
    ),
    work_fn=_workload,
)


def handle_message(payload: Dict[str, object]) -> Dict[str, object]:
    return _MODULE.handler(payload)


def run() -> None:
    _MODULE.run()


__all__ = ["handle_message", "run"]
