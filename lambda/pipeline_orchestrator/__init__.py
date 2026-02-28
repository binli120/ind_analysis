# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

"""Package exposing the pipeline orchestrator Lambda handler."""

# Expose Lambda handler for convenience.
from .app import handler

__all__ = ["handler"]
