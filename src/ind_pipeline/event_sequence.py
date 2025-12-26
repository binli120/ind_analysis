# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""
Defines the canonical sequencing of pipeline stages. This is primarily used for
orchestration tooling and for informational purposes when wiring SNS topics.
"""

from __future__ import annotations

from typing import Dict, List

STAGE_SEQUENCE: List[str] = [
    "pdf-extraction",
    "zeroshot-labeling",
    "pdf-parsing-chunking",
    "metadata-summary-extraction",
]

# Some stages can fan out to multiple downstream consumers (for example, PDF
# extraction can feed both labeling and section parsing). This successor map
# represents the intended fan-out, allowing infrastructure tooling to connect
# completion topics to the correct downstream inputs.
STAGE_SUCCESSORS: Dict[str, List[str]] = {
    "pdf-extraction": ["zeroshot-labeling", "pdf-parsing-chunking"],
    "zeroshot-labeling": [],
    "pdf-parsing-chunking": ["metadata-summary-extraction"],
    "metadata-summary-extraction": [],
}

__all__ = ["STAGE_SEQUENCE", "STAGE_SUCCESSORS"]
