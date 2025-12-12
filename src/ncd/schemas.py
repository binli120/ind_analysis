from typing import List, Optional

from pydantic import BaseModel


class DoseGroupSchema(BaseModel):
    """Exposure dose group.

    Schemas for NCD data.
    @author: Bin Lee
    @email: blee@filynai.com
    """

    name: Optional[str]
    sex: Optional[str]
    n_animals: Optional[int]
    dose_mg_per_kg: Optional[float]
    dose_mg_per_m2: Optional[float]


class ExposureMetricSchema(BaseModel):
    """
    Expose Metrics
    """

    dose_group_name: Optional[str]
    parameter: str  # Cmax, AUC, t_half
    value: float
    unit: str
    timepoint: Optional[str]
    clinical_multiple: Optional[float]


class FindingSchema(BaseModel):
    organ_system: Optional[str]
    organ: Optional[str]
    finding_term: str
    severity: Optional[str]
    adverse: Optional[bool]
    reversible: Optional[bool]
    onset_day: Optional[int]
    dose_threshold_mg_per_kg: Optional[float]
    noael_flag: Optional[bool]


class ToxStudySummarySchema(BaseModel):
    study_id: str
    species: Optional[str]
    route: Optional[str]
    duration_days: Optional[int]
    noael_mg_per_kg: Optional[float]
    loael_mg_per_kg: Optional[float]
    limiting_organ: Optional[str]
    limiting_finding: Optional[str]
    clinical_multiple: Optional[float]
    dose_groups: List[DoseGroupSchema]
    exposure_metrics: List[ExposureMetricSchema]
    findings: List[FindingSchema]
    source_chunk_ids: List[str]


class PKStudySummarySchema(BaseModel):
    study_id: str
    species: Optional[str]
    route: Optional[str]
    parameters: List[ExposureMetricSchema]
    source_chunk_ids: List[str]
