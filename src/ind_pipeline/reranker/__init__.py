from __future__ import annotations

from typing import Dict

from ind_pipeline.stage_common import SimpleStageModule, StageModuleSpec


def _workload(payload: Dict[str, object]) -> Dict[str, object]:
    query = payload.get("query")
    return {
        "notes": "Placeholder reranking stage.",
        "query": query,
    }


_MODULE = SimpleStageModule(
    StageModuleSpec(
        module_name="reranker",
        env_prefix="RERANKER",
        description="Rank evidence for regulatory credibility and emit curated sets.",
        default_queue_name="reranker-queue",
    ),
    work_fn=_workload,
)


def handle_message(payload: Dict[str, object]) -> Dict[str, object]:
    return _MODULE.handler(payload)


def run() -> None:
    _MODULE.run()


__all__ = ["handle_message", "run"]
