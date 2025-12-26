# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Package exposing the pipeline orchestrator Lambda handler."""

# Expose Lambda handler for convenience.
from .app import handler

__all__ = ["handler"]
