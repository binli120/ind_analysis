# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Helpers for running pipeline jobs concurrently across multiple PDFs."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

from .config import PipelineConfig
from .pipeline import PDFProcessingPipeline, PipelineResult

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PipelineTaskResult:
    """Represents the outcome of processing a single PDF."""

    pdf_path: Path
    result: Optional[PipelineResult]
    error: Optional[BaseException]
    duration_seconds: float

    @property
    def succeeded(self) -> bool:
        return self.error is None


class PipelineRunner:
    """Runs multiple PDF pipelines concurrently using a thread pool."""

    def __init__(
        self,
        config: Optional[PipelineConfig] = None,
        *,
        max_workers: Optional[int] = None,
        pipeline_factory: Optional[Callable[[], PDFProcessingPipeline]] = None,
    ) -> None:
        self._config = config or PipelineConfig()
        self._max_workers = max_workers
        self._pipeline_factory = pipeline_factory or self._default_factory

    def _default_factory(self) -> PDFProcessingPipeline:
        """Instantiate a pipeline per worker to preserve thread-safety."""
        return PDFProcessingPipeline(config=self._config)

    def run_many(self, pdf_paths: Iterable[Path]) -> List[PipelineTaskResult]:
        """Process many PDFs concurrently and return their results."""
        paths: List[Path] = [Path(p) for p in pdf_paths]
        if not paths:
            return []

        results: List[PipelineTaskResult] = []
        logger.info(
            "[pipeline-runner] processing %d PDFs with %s workers",
            len(paths),
            self._max_workers or "default",
        )

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            future_map = {
                executor.submit(self._run_single, path): path for path in paths
            }
            for future in as_completed(future_map):
                path = future_map[future]
                try:
                    result = future.result()
                    results.append(result)
                    logger.debug(
                        "[pipeline-runner] completed %s success=%s duration=%.2fs",
                        path.name,
                        result.succeeded,
                        result.duration_seconds,
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.exception(
                        "[pipeline-runner] unexpected failure processing %s", path
                    )
                    results.append(
                        PipelineTaskResult(
                            pdf_path=path,
                            result=None,
                            error=exc,
                            duration_seconds=0.0,
                        )
                    )

        return results

    def _run_single(self, pdf_path: Path) -> PipelineTaskResult:
        """Execute the pipeline for a single PDF and capture timing/errors."""
        start = time.perf_counter()
        pipeline = self._pipeline_factory()
        try:
            result = pipeline.run(pdf_path)
            error: Optional[BaseException] = None
        except (
            BaseException
        ) as exc:  # capture all exceptions, propagate later if needed
            logger.warning(
                "[pipeline-runner] pipeline failed for %s: %s", pdf_path.name, exc
            )
            result = None
            error = exc
        duration = time.perf_counter() - start
        return PipelineTaskResult(
            pdf_path=pdf_path,
            result=result,
            error=error,
            duration_seconds=duration,
        )


__all__: Sequence[str] = [
    "PipelineRunner",
    "PipelineTaskResult",
]
