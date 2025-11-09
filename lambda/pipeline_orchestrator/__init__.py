"""Package exposing the pipeline orchestrator Lambda handler."""

# Expose Lambda handler for convenience.
from .app import handler

__all__ = ["handler"]
