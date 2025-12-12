from typing import Dict, List, Optional

from pydantic import BaseModel


class CTDTarget(BaseModel):
    """Represents one CTD section target."""

    section: str
    weight: float = 1.0  # used for ranking


class MappingRule(BaseModel):
    """One rule for a Module 4 → CTD mapping entry."""

    module4_section: str
    title: str
    keywords: List[str]
    maps_to: List[str]  # list of CTD sections
    category: Optional[str] = None  # tox, pk, tk, safety-pharm, etc.


class MappingResult(BaseModel):
    """Output from a mapping call."""

    ctd_sections: List[str]
    score_breakdown: Dict[str, float]
    matched_keywords: List[str]
    module4_section: str
