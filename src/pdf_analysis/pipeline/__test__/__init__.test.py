# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Tests for src/pdf_analysis/pipeline/__init__.py."""

from __future__ import annotations

import ast
from pathlib import Path


def _load_module_ast() -> ast.Module:
    init_path = Path(__file__).resolve().parents[1] / "__init__.py"
    source = init_path.read_text(encoding="utf-8")
    return ast.parse(source)


def _extract_all_names(module_ast: ast.Module) -> list[str]:
    for node in module_ast.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        names: list[str] = []
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(
                                elt.value, str
                            ):
                                names.append(elt.value)
                        return names
    return []


def test_pipeline_init_docstring_present() -> None:
    module_ast = _load_module_ast()
    doc = ast.get_docstring(module_ast)
    assert doc and doc.strip()


def test_pipeline_init_exports_expected_symbols() -> None:
    names = set(_extract_all_names(_load_module_ast()))
    expected = {
        "PipelineConfig",
        "PDFProcessingPipeline",
        "PipelineRunner",
        "PipelineTaskResult",
    }
    assert expected.issubset(names)
