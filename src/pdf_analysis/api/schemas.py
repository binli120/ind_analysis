"""Request schemas used by API routes."""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from pdf_analysis.api.constants import USER_PROMPT_MAX_CHARS


class S3AnalyzeRequest(BaseModel):
    bucket: str
    key: str
    version_id: Optional[str] = None
    aws_region: Optional[str] = None


class S3MarkdownRequest(BaseModel):
    bucket: str
    key: str
    version_id: Optional[str] = None
    aws_region: Optional[str] = None


class S3MarkdownUploadRequest(BaseModel):
    bucket: str
    path: str
    filename: str
    markdown: str
    label: Optional[str] = None
    tags: Optional[Dict[str, str]] = None
    metadata: Optional[Dict[str, str]] = None
    aws_region: Optional[str] = None


class S3NewProjectRequest(BaseModel):
    bucket: str
    tenant_name: str
    project_name: str
    aws_region: Optional[str] = None


class NCDLabelRequest(BaseModel):
    """Request payload for minimal section labeling of a PDF stored in S3."""

    key: str
    company: str
    project: str
    bucket: Optional[str] = None
    aws_region: Optional[str] = None
    page_limit: int = 5
    use_llm: bool = False
    section_scope: str = "auto"


class NCDRelabelRequest(BaseModel):
    """Request payload to correct a labeled section for a PDF in S3."""

    key: str
    company: str
    project: str
    section_number: str
    section_title: Optional[str] = None
    bucket: Optional[str] = None
    aws_region: Optional[str] = None


class TemplateOverrideRequest(BaseModel):
    """Create or update a template override for a user."""

    user_id: str
    section: str
    subsection: Optional[str] = None
    payload: Dict[str, Any]
    aws_region: Optional[str] = None


class CTDSectionSummaryRequest(BaseModel):
    """Request payload for generating a CTD section summary draft."""

    section: str
    tenant_id: str
    project_id: str
    bucket: str
    user_prompt: Optional[str] = Field(
        default=None,
        max_length=USER_PROMPT_MAX_CHARS,
    )
    user_comment: Optional[str] = None
    previous_summary_id: Optional[str] = None
    refresh_template: bool = False


class CTDSectionSummaryApproveRequest(BaseModel):
    """Request payload for approving a CTD section summary."""

    tenant_id: str
    project_id: str
    bucket: str
    final_text: str
    summary_id: Optional[str] = None
    section: Optional[str] = None


class CTDTabulatedSummaryRequest(BaseModel):
    """Request payload for generating a CTD tabulated summary draft."""

    section: str
    tenant_id: str
    project_id: str
    bucket: str
    use_llm: Optional[bool] = True
    user_prompt: Optional[str] = Field(
        default=None,
        max_length=USER_PROMPT_MAX_CHARS,
    )
    user_comment: Optional[str] = None
    previous_tabulated_id: Optional[str] = None
    refresh_template: bool = False


class CTDTabulatedSummaryApproveRequest(BaseModel):
    """Request payload for approving a CTD tabulated summary."""

    tenant_id: str
    project_id: str
    bucket: str
    final_payload: Dict[str, Any]
    summary_id: Optional[str] = None
    section: Optional[str] = None


class NCDGapAnalysisRequest(BaseModel):
    """Request payload for project-level gap analysis."""

    project_id: str
    bucket: str
    tenant_id: Optional[str] = None
    project_prefix: Optional[str] = None
    include_optional_p1: bool = False
    aws_region: Optional[str] = None
