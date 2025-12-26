# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Unit tests for pdf_analysis.pipeline.executor."""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from pdf_analysis.pipeline.executor import PipelineRunner, PipelineTaskResult


def test_pipeline_task_result_succeeded() -> None:
    result = PipelineTaskResult(pdf_path=Path("a.pdf"), result=None, error=None, duration_seconds=0.1)
    assert result.succeeded is True
    failed = PipelineTaskResult(pdf_path=Path("b.pdf"), result=None, error=RuntimeError("fail"), duration_seconds=0.1)
    assert failed.succeeded is False


def test_pipeline_runner_run_many_executes(tmp_path: Path) -> None:
    calls: List[Path] = []

    class FakePipeline:
        def run(self, pdf_path: Path):
            calls.append(pdf_path)
            return "ok"

    runner = PipelineRunner(pipeline_factory=lambda: FakePipeline())
    paths = [tmp_path / "a.pdf", tmp_path / "b.pdf"]
    results = runner.run_many(paths)

    assert len(results) == 2
    assert {r.pdf_path for r in results} == set(paths)
    assert all(r.succeeded for r in results)
    assert set(calls) == set(paths)


def test_pipeline_runner_handles_errors(tmp_path: Path) -> None:
    class FakePipeline:
        def run(self, _pdf_path: Path):
            raise RuntimeError("boom")

    runner = PipelineRunner(pipeline_factory=lambda: FakePipeline())
    result = runner.run_many([tmp_path / "a.pdf"])[0]

    assert result.succeeded is False
    assert isinstance(result.error, RuntimeError)
