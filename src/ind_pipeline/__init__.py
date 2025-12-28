# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""IND pipeline package that wires stage modules into the central registry."""

# ind_pipeline package initialiser.
#
# Importing modules here ensures they self-register with the central registry.

from .registry import MODULE_REGISTRY

# Core modules
from . import pdf_extraction  # noqa: F401
from . import zeroshot_labeling  # noqa: F401
from . import pdf_parsing_chunking  # noqa: F401
from . import metadata_summary_extraction  # noqa: F401

__all__ = ["MODULE_REGISTRY"]
