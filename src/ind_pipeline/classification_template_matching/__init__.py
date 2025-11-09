from __future__ import annotations

from typing import Dict

from ind_pipeline.stage_common import SimpleStageModule, StageModuleSpec


def _workload(payload: Dict[str, object]) -> Dict[str, object]:
    labels = payload.get("labels")
    label_count = len(labels) if isinstance(labels, list) else 0
    return {
        "notes": "Placeholder classification and template matching executed.",
        "label_count": label_count,
    }


_MODULE = SimpleStageModule(
    StageModuleSpec(
        module_name="classification-template-matching",
        env_prefix="CLASSIFICATION_TEMPLATE_MATCHING",
        description="Apply IND section classifiers and template matching to content chunks.",
        default_queue_name="classification-template-matching-queue",
    ),
    work_fn=_workload,
)


def handle_message(payload: Dict[str, object]) -> Dict[str, object]:
    return _MODULE.handler(payload)


def run() -> None:
    _MODULE.run()


__all__ = ["handle_message", "run"]
