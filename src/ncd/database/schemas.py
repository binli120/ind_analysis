# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

from typing import List, Optional

from pydantic import BaseModel, Field


class DoseGroupSchema(BaseModel):
    """Exposure dose group.

    Schemas for NCD data.
    @author: Bin Lee
    @email: blee@longooc.com
    """

    name: Optional[str] = None
    sex: Optional[str] = None
    n_animals: Optional[int] = None
    dose_mg_per_kg: Optional[float] = None
    dose_mg_per_m2: Optional[float] = None


class ExposureMetricSchema(BaseModel):
    """
    Expose Metrics
    """

    dose_group_name: Optional[str] = None
    parameter: Optional[str] = None  # Cmax, AUC, t_half
    value: Optional[float] = None
    unit: Optional[str] = None
    timepoint: Optional[str] = None
    clinical_multiple: Optional[float] = None


class FindingSchema(BaseModel):
    organ_system: Optional[str] = None
    organ: Optional[str] = None
    finding_term: str
    severity: Optional[str] = None
    adverse: Optional[bool] = None
    reversible: Optional[bool] = None
    onset_day: Optional[int] = None
    dose_threshold_mg_per_kg: Optional[float] = None
    noael_flag: Optional[bool] = None


class ToxStudySummarySchema(BaseModel):
    study_id: str
    species: Optional[str] = None
    route: Optional[str] = None
    duration_days: Optional[int] = None
    noael_mg_per_kg: Optional[float] = None
    loael_mg_per_kg: Optional[float] = None
    limiting_organ: Optional[str] = None
    limiting_finding: Optional[str] = None
    clinical_multiple: Optional[float] = None
    dose_groups: List[DoseGroupSchema] = Field(default_factory=list)
    exposure_metrics: List[ExposureMetricSchema] = Field(default_factory=list)
    findings: List[FindingSchema] = Field(default_factory=list)
    source_chunk_ids: List[str] = Field(default_factory=list)


class PKStudySummarySchema(BaseModel):
    study_id: str
    species: Optional[str] = None
    route: Optional[str] = None
    parameters: List[ExposureMetricSchema] = Field(default_factory=list)
    source_chunk_ids: List[str] = Field(default_factory=list)
