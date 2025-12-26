# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""
LangChain orchestration helpers for IND Module 4 → NCD persistence.

This module wires:
  - Study segmentation (multi-study PDFs)
  - Task-specific extractors (NOAEL / PK)
  - Anchor creation for traceability
  - Composite confidence scoring
  - Strict persistence order into extraction_runs, NCD tables, and extracted_entities

LLMs never write directly to NCD tables; this layer mediates writes and can
optionally create review comments for low-confidence results.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional
from uuid import uuid4

from .pipeline import DocumentChunk
from ncd.db_interface import NCDRepository

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class StudySegment:
    study_id: str
    study_type: str
    start_page: int
    end_page: int
    species: Optional[str] = None
    route: Optional[str] = None
    duration: Optional[str] = None


@dataclass(slots=True)
class Anchor:
    page: int
    start_offset: int
    end_offset: int
    quote: str
    bbox: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page": self.page,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "quote": self.quote,
            "bbox": self.bbox,
        }


@dataclass(slots=True)
class NOAELResult:
    dose: Optional[float]
    dose_unit: Optional[str]
    species: Optional[str]
    sex: Optional[str]
    endpoint: Optional[str]
    quote: str
    confidence: Optional[float]


@dataclass(slots=True)
class PKResult:
    parameter: str
    value: Optional[float]
    unit: Optional[str]
    dose_group: Optional[str]
    quote: str
    confidence: Optional[float]


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
class LangChainExtractionPipeline:
    """
    Execute LangChain extractors with traceable persistence into NCD.

    Chains are passed in (already configured with LLM + prompt + structured output).
    Each chain must expose `.invoke(input_dict)` and return structured dicts or lists.
    """

    def __init__(
        self,
        *,
        study_segmentation_chain: Any,
        noael_chain: Any,
        pk_chain: Optional[Any] = None,
        repository: Optional[NCDRepository] = None,
        low_confidence_threshold: float = 0.75,
    ) -> None:
        self.study_segmentation_chain = study_segmentation_chain
        self.noael_chain = noael_chain
        self.pk_chain = pk_chain
        self.repo = repository or NCDRepository()
        self.low_confidence_threshold = low_confidence_threshold

    # ------------------------------------------------------------------ #
    # Public entrypoint
    # ------------------------------------------------------------------ #
    def run(
        self,
        *,
        document_version_id: str,
        source_document_id: str,
        extractor_name: str,
        model_name: str,
        chunks: List[DocumentChunk],
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Orchestrate segmentation → extraction → confidence → persistence.
        """
        run_id = self.repo.create_extraction_run(
            document_version_id=document_version_id,
            extractor=extractor_name,
            model=model_name,
            status="completed",
            created_by=created_by,
        )

        study_segments = self.segment_studies(chunks)
        persisted: Dict[str, Any] = {
            "extraction_run_id": run_id,
            "studies": [],
        }

        for segment in study_segments:
            study_db_id = self.repo.upsert_study(
                study_id=segment.study_id,
                study_type=segment.study_type,
                source_document_id=source_document_id,
                species=segment.species,
                route=segment.route,
                duration=segment.duration,
            )
            study_chunks = [
                chunk
                for chunk in chunks
                if segment.start_page <= chunk.page <= segment.end_page
            ]

            noael_records = self.extract_noael(study_chunks)
            pk_records = self.extract_pk(study_chunks) if self.pk_chain else []

            study_result = {
                "study_id": segment.study_id,
                "db_id": study_db_id,
                "noael": [],
                "pk": [],
            }

            for record, chunk in noael_records:
                confidence = self._composite_confidence(
                    {
                        "llm_confidence": record.confidence,
                    }
                )
                anchor = self._make_anchor(chunk, record.quote)
                noael_id = self.repo.insert_noael(
                    study_id=study_db_id,
                    dose=record.dose,
                    dose_unit=record.dose_unit,
                    species=record.species,
                    sex=record.sex,
                    endpoint=record.endpoint,
                    value=record.quote,
                )
                self.repo.insert_extracted_entity(
                    extraction_run_id=run_id,
                    document_version_id=document_version_id,
                    entity_type="NOAEL",
                    entity_id=noael_id,
                    anchor=anchor.to_dict(),
                    confidence=confidence,
                )
                if confidence is not None and confidence < self.low_confidence_threshold:
                    self.repo.create_low_confidence_comment(
                        document_version_id=document_version_id,
                        anchor=anchor.to_dict(),
                        content="⚠️ Low-confidence NOAEL extraction. Please review.",
                        created_by=created_by,
                    )
                study_result["noael"].append({"id": noael_id, "confidence": confidence})

            for record, chunk in pk_records:
                confidence = self._composite_confidence(
                    {
                        "llm_confidence": record.confidence,
                    }
                )
                anchor = self._make_anchor(chunk, record.quote)
                pk_id = self.repo.insert_pk_parameter(
                    study_id=study_db_id,
                    parameter=record.parameter,
                    value=record.value,
                    unit=record.unit,
                    dose_group=record.dose_group,
                )
                self.repo.insert_extracted_entity(
                    extraction_run_id=run_id,
                    document_version_id=document_version_id,
                    entity_type="PK_PARAM",
                    entity_id=pk_id,
                    anchor=anchor.to_dict(),
                    confidence=confidence,
                )
                if confidence is not None and confidence < self.low_confidence_threshold:
                    self.repo.create_low_confidence_comment(
                        document_version_id=document_version_id,
                        anchor=anchor.to_dict(),
                        content="⚠️ Low-confidence PK extraction. Please review.",
                        created_by=created_by,
                    )
                study_result["pk"].append({"id": pk_id, "confidence": confidence})

            persisted["studies"].append(study_result)

        return persisted

    # ------------------------------------------------------------------ #
    # Chains
    # ------------------------------------------------------------------ #
    def segment_studies(self, chunks: List[DocumentChunk]) -> List[StudySegment]:
        """
        Run the study segmentation chain to identify per-study page spans.
        """
        joined_text = self._render_chunks_for_prompt(chunks)
        response = self.study_segmentation_chain.invoke({"context": joined_text})
        raw_segments = self._normalize_list(response, field_name="studies")
        segments: List[StudySegment] = []
        for raw in raw_segments:
            try:
                segment = StudySegment(
                    study_id=raw.get("study_id") if isinstance(raw, dict) else getattr(raw, "study_id", None) or f"study-{uuid4()}",
                    study_type=raw.get("study_type") if isinstance(raw, dict) else getattr(raw, "study_type", None) or "tox",
                    start_page=int(raw.get("start_page", 1)) if isinstance(raw, dict) else int(getattr(raw, "start_page", 1)),
                    end_page=int(raw.get("end_page", 1)) if isinstance(raw, dict) else int(getattr(raw, "end_page", 1)),
                    species=raw.get("species") if isinstance(raw, dict) else getattr(raw, "species", None),
                    route=raw.get("route") if isinstance(raw, dict) else getattr(raw, "route", None),
                    duration=raw.get("duration") if isinstance(raw, dict) else getattr(raw, "duration", None),
                )
                segments.append(segment)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Skipping malformed study segment %s: %s", raw, exc)
        return segments

    def extract_noael(
        self, study_chunks: Iterable[DocumentChunk]
    ) -> List[tuple[NOAELResult, DocumentChunk]]:
        results: List[tuple[NOAELResult, DocumentChunk]] = []
        for chunk in study_chunks:
            response = self.noael_chain.invoke({"text": chunk.text})
            payloads = self._normalize_list(response, field_name="items")
            if not payloads:
                continue
            for payload in payloads:
                try:
                    result = NOAELResult(
                        dose=payload.get("dose"),
                        dose_unit=payload.get("dose_unit"),
                        species=payload.get("species"),
                        sex=payload.get("sex"),
                        endpoint=payload.get("endpoint"),
                        quote=payload.get("quote", "").strip(),
                        confidence=payload.get("confidence"),
                    )
                    results.append((result, chunk))
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("Skipping malformed NOAEL payload %s: %s", payload, exc)
        return results

    def extract_pk(
        self, study_chunks: Iterable[DocumentChunk]
    ) -> List[tuple[PKResult, DocumentChunk]]:
        if not self.pk_chain:
            return []
        results: List[tuple[PKResult, DocumentChunk]] = []
        for chunk in study_chunks:
            response = self.pk_chain.invoke({"text": chunk.text})
            payloads = self._normalize_list(response, field_name="items")
            if not payloads:
                continue
            for payload in payloads:
                try:
                    result = PKResult(
                        parameter=payload.get("parameter"),
                        value=payload.get("value"),
                        unit=payload.get("unit"),
                        dose_group=payload.get("dose_group"),
                        quote=payload.get("quote", "").strip(),
                        confidence=payload.get("confidence"),
                    )
                    results.append((result, chunk))
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("Skipping malformed PK payload %s: %s", payload, exc)
        return results

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _make_anchor(self, chunk: DocumentChunk, quote: str) -> Anchor:
        return Anchor(
            page=chunk.page,
            start_offset=chunk.offset_start,
            end_offset=chunk.offset_end,
            quote=quote,
            bbox=getattr(chunk, "bbox", None),
        )

    def _composite_confidence(self, scores: Dict[str, Optional[float]]) -> Optional[float]:
        """
        Composite confidence using weighted mean; skips missing scores.
        """
        weights = {
            "llm_confidence": 0.5,
            "citation_exact_match_score": 0.2,
            "value_normalization_score": 0.2,
            "cross_chunk_consistency_score": 0.1,
        }
        total_weight = 0.0
        weighted_sum = 0.0
        for key, weight in weights.items():
            score = scores.get(key)
            if score is None:
                continue
            weighted_sum += weight * score
            total_weight += weight
        if total_weight == 0:
            return None
        return weighted_sum / total_weight

    def _render_chunks_for_prompt(self, chunks: Iterable[DocumentChunk]) -> str:
        """
        Render chunks with page markers for study segmentation context.
        """
        lines: List[str] = []
        for chunk in chunks:
            lines.append(f"[page={chunk.page} chunk={chunk.chunk_id}] {chunk.text}")
        return "\n\n".join(lines)

    def _normalize_list(self, response: Any, field_name: str | None = None) -> List[Any]:
        """
        Accept list, BaseModel, or dict payloads and return a list of dict-like items.
        """
        if response is None:
            return []
        # LangChain structured output may return a pydantic BaseModel
        if hasattr(response, "dict"):
            data = response.dict()
            if field_name and field_name in data:
                value = data.get(field_name) or []
                return value if isinstance(value, list) else [value]
            # if the model itself is a list wrapper, pick the first key
            if len(data) == 1:
                sole_value = next(iter(data.values()))
                return sole_value if isinstance(sole_value, list) else [sole_value]
            return [data]
        if isinstance(response, dict):
            if field_name and field_name in response:
                value = response.get(field_name) or []
                return value if isinstance(value, list) else [value]
            return [response]
        if isinstance(response, list):
            return response
        return []
