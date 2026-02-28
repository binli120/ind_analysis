# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Unit tests for pdf_analysis.pipeline.config."""

from __future__ import annotations

from pdf_analysis.pipeline import config


def test_default_pipeline_config_has_nested_defaults() -> None:
    cfg = config.PipelineConfig()
    assert cfg.text.engines
    assert cfg.ocr.enable is True
    assert cfg.structured.table_engines
    assert cfg.llm.model
    assert cfg.redis.host


def test_default_pipeline_config_singleton() -> None:
    default = config.DEFAULT_PIPELINE_CONFIG
    assert isinstance(default, config.PipelineConfig)
    assert default is config.DEFAULT_PIPELINE_CONFIG
