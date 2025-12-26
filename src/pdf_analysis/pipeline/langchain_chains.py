# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""
Ready-to-use LangChain chains for study segmentation, NOAEL, and PK extraction.

These are intentionally focused, structured-output chains aligned with the
NCD schema and traceability requirements.

Usage:
    from pdf_analysis.pipeline.langchain_chains import (
        build_study_segmentation_chain,
        build_noael_chain,
        build_pk_chain,
    )
    seg_chain = build_study_segmentation_chain()
    noael_chain = build_noael_chain()
    pk_chain = build_pk_chain()
"""

from __future__ import annotations

from typing import Any, List, Optional


def _load_chat_model(model: str = "gpt-4o-mini", temperature: float = 0.0):
    try:
        from langchain_openai import ChatOpenAI
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "langchain_openai is required for the default chat model; install with `poetry install --with llm`."
        ) from exc

    return ChatOpenAI(model=model, temperature=temperature)


def _pydantic():
    try:
        from pydantic import BaseModel, Field
    except ModuleNotFoundError:  # pragma: no cover - fallback
        from langchain_core.pydantic_v1 import BaseModel, Field
    return BaseModel, Field


def build_study_segmentation_chain(
    *,
    llm: Any | None = None,
    model: str = "gpt-4o-mini",
    temperature: float = 0.0,
):
    """Identify individual studies inside a Module 4 PDF."""
    try:
        from langchain_core.prompts import ChatPromptTemplate
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("langchain_core is required to build prompts") from exc

    BaseModel, Field = _pydantic()

    class StudySegmentSchema(BaseModel):
        study_id: str = Field(..., description="Study identifier")
        study_type: str = Field(..., description="Study type (e.g., repeat_dose_tox)")
        start_page: int = Field(..., description="1-based start page")
        end_page: int = Field(..., description="1-based end page")
        species: Optional[str] = Field(None, description="Species if stated")
        route: Optional[str] = Field(None, description="Route if stated")
        duration: Optional[str] = Field(None, description="Duration if stated")

    class StudyListSchema(BaseModel):
        studies: List[StudySegmentSchema] = Field(default_factory=list)

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are segmenting an IND Module 4 PDF into distinct studies. "
                "Use only explicit page cues and headings. "
                "Return a JSON object with a `studies` array; omit inferred fields.",
            ),
            (
                "human",
                "Document chunks:\n{context}\n\n"
                "Rules:\n"
                "- Use 1-based page numbers.\n"
                "- If a field is not explicitly supported, omit it.\n"
                "- start_page/end_page must be within the PDF range.\n"
                "- study_type examples: repeat_dose_tox, tk, pk, safety_pharm.\n"
                "Return structured output.",
            ),
        ]
    )

    chat = llm or _load_chat_model(model=model, temperature=temperature)
    return prompt | chat.with_structured_output(StudyListSchema)


def build_noael_chain(
    *,
    llm: Any | None = None,
    model: str = "gpt-4o-mini",
    temperature: float = 0.0,
):
    """Extract NOAEL rows aligned with ncd_noael."""
    try:
        from langchain_core.prompts import ChatPromptTemplate
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("langchain_core is required to build prompts") from exc

    BaseModel, Field = _pydantic()

    class NOAELSchema(BaseModel):
        dose: Optional[float] = Field(None, description="Dose numeric value")
        dose_unit: Optional[str] = Field(None, description="Unit for dose (e.g., mg/kg)")
        species: Optional[str] = Field(None, description="Species explicitly stated")
        sex: Optional[str] = Field(None, description="Sex if specified")
        endpoint: Optional[str] = Field(None, description="Endpoint/context for the NOAEL")
        quote: str = Field(..., description="Verbatim citation for the NOAEL statement")
        confidence: Optional[float] = Field(
            None, description="LLM confidence 0-1 if available"
        )

    class NOAELList(BaseModel):
        items: List[NOAELSchema] = Field(default_factory=list)

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You extract NOAEL data from toxicology studies. "
                "Return ONLY values explicitly stated and include verbatim citation text. "
                "Do not infer doses or species.",
            ),
            (
                "human",
                "Context:\n{text}\n\n"
                "Return structured NOAEL rows. If none are explicitly stated, return an empty list.",
            ),
        ]
    )

    chat = llm or _load_chat_model(model=model, temperature=temperature)
    return prompt | chat.with_structured_output(NOAELList)


def build_pk_chain(
    *,
    llm: Any | None = None,
    model: str = "gpt-4o-mini",
    temperature: float = 0.0,
):
    """Extract PK parameters aligned with ncd_pk_parameters."""
    try:
        from langchain_core.prompts import ChatPromptTemplate
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("langchain_core is required to build prompts") from exc

    BaseModel, Field = _pydantic()

    class PKSchema(BaseModel):
        parameter: str = Field(..., description="PK parameter e.g., AUC, Cmax, Tmax, t1/2, CL, Vd")
        value: Optional[float] = Field(None, description="Numeric value for the parameter")
        unit: Optional[str] = Field(None, description="Unit for the parameter")
        dose_group: Optional[str] = Field(None, description="Dose group or arm label, if stated")
        quote: str = Field(..., description="Verbatim citation for the PK value")
        confidence: Optional[float] = Field(None, description="LLM confidence 0-1 if available")

    class PKList(BaseModel):
        items: List[PKSchema] = Field(default_factory=list)

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You extract PK parameters from Module 4 studies. "
                "Return only explicitly stated values with verbatim citations. "
                "Valid parameters: AUC, Cmax, Tmax, t1/2, CL, Vd.",
            ),
            (
                "human",
                "Context:\n{text}\n\n"
                "Return structured PK rows. If none are present, return an empty list.",
            ),
        ]
    )

    chat = llm or _load_chat_model(model=model, temperature=temperature)
    return prompt | chat.with_structured_output(PKList)
