# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""
Central registry enumerating all available independent pipeline modules.

The registry makes it easy for orchestration tooling (CLI, tests, deployment
scripts) to discover the module entry points without importing each module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict


@dataclass(frozen=True, slots=True)
class ModuleDescriptor:
    """Metadata describing a pipeline module."""

    name: str
    description: str
    entrypoint: Callable[[], None]


MODULE_REGISTRY: Dict[str, ModuleDescriptor] = {}


def register_module(descriptor: ModuleDescriptor) -> None:
    """
    Register a module descriptor. If a module with the same name already exists
    it will be replaced, which allows override in tests.
    """
    MODULE_REGISTRY[descriptor.name] = descriptor
