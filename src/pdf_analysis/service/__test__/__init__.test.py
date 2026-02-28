# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Tests for src/pdf_analysis/service/__init__.py."""

from __future__ import annotations

import ast
from pathlib import Path


def _load_module_ast() -> ast.Module:
    init_path = Path(__file__).resolve().parents[1] / "__init__.py"
    source = init_path.read_text(encoding="utf-8")
    return ast.parse(source)


def test_service_init_docstring_present() -> None:
    module_ast = _load_module_ast()
    doc = ast.get_docstring(module_ast)
    assert doc and doc.strip()
