# LangChain Extraction Pipeline (IND Module 4 → NCD → CTD)

Production-grade pipeline goals:
- Regulatory traceability and replayable extraction runs.
- Immutable sources; every NCD value has an anchor back to a document version.
- LLMs never write directly to NCD tables; human review is first-class.

## High-Level Architecture
PDF / DOCX (Module 4) → Document Loader → Layout-Aware Chunking → Study Segmentation → Task-Specific Extractors → Entity Normalization → Confidence Scoring → DB Persistence (Extraction → NCD → Trace)

## Core Design Principles
- Documents are immutable sources, versioned via `document_versions`.
- Extraction is replayable; each run is captured in `extraction_runs`.
- Every NCD record is traceable via `extracted_entities` anchors.
- LLMs emit structured payloads; DB writes happen in controlled stages.
- Human review/validation (`ncd_validation`) is first-class.

## Step 1 — Document Ingestion Layer
Goal: Convert PDFs into layout-aware, anchorable chunks for LangChain.

Tools:
- pdfplumber / PyMuPDF
- OCR fallback via ocrmypdf
- Optional: layout-parser

Internal output shape:
```json
{
  "chunk_id": "uuid",
  "page": 3,
  "text": "...",
  "bbox": null,
  "offset_start": 12345,
  "offset_end": 12670
}
```

Chunking guidance:
- Do **not** split arbitrarily; prefer headings, tables, figure captions, and study boundaries.
- Keep per-page grouping; maintain offsets for downstream highlighting.
- Emit tables as their own chunks (CSV rendering) with page metadata.

### Local dev check (chunking + DB seed)
1) Apply the schema, then run the smoke seed:
```shell
psql "$DATABASE_URL" -f scripts/ncd_schema_smoke_test.sql
```
2) Run the pipeline on a sample PDF to emit LangChain chunks (stored in `PipelineResult.extras["document_chunks"]`):
```shell
poetry run python - <<'PY'
from pathlib import Path
from pdf_analysis.pipeline.pipeline import PDFProcessingPipeline

pipeline = PDFProcessingPipeline()
result = pipeline.run(Path("sample.pdf"))
print(len(result.extras.get("document_chunks", [])), "chunks")
print(result.extras.get("document_chunks", [])[:2])  # preview first two anchors
PY
```
3) Inspect anchors for traceability: chunk IDs, page numbers, and offsets should be present; table chunks will include CSV content.

---

## Step 2 — Study Segmentation Chain (critical)
Purpose: identify individual studies inside a single Module 4 PDF so we can write multiple `ncd_studies` rows per PDF.

Chain shape:
```python
study_segmentation_chain = (
    PromptTemplate(...)
    | llm.with_structured_output(StudyListSchema)
)
```

Expected output:
```json
[
  {"study_id": "Study XYZ-001", "study_type": "repeat_dose_tox", "start_page": 12, "end_page": 45}
]
```

## Step 3 — Task-Specific Extractors (not one giant prompt)
- NOAEL Extractor → `ncd_noael`
- PK Extractor → `ncd_pk_parameters`
- Dose Group Extractor → (future)
- Species/Route → `ncd_studies`

Example NOAEL chain:
```python
noael_chain = (
  PromptTemplate(
    template=\"\"\"
You are extracting NOAEL data from a toxicology study.

Return ONLY values explicitly stated.
Include citation text verbatim.

{text}
\"\"\",
    input_variables=["text"],
  )
  | llm.with_structured_output(NOAELSchema)
)
```

Structured schema:
```python
class NOAELSchema(BaseModel):
    dose: float
    dose_unit: str
    species: str
    sex: str | None
    endpoint: str
    quote: str
    confidence: float
```

## Step 4 — Anchor Construction (source of truth)
Every extracted value produces:
```python
anchor = {
  "page": chunk.page,
  "start_offset": chunk.offset_start,
  "end_offset": chunk.offset_end,
  "quote": noael.quote,
  "bbox": chunk.bbox,
}
```

Anchors populate:
- `extracted_entities.anchor`
- `document_comments.anchor`

## Step 5 — Confidence Scoring
Composite confidence, not just LLM self-confidence:
```python
confidence = weighted_mean([
    llm_confidence,
    citation_exact_match_score,
    value_normalization_score,
    cross_chunk_consistency_score,
])
```

Route low-confidence items (<0.75) to human review with an auto comment.

## Step 6 — Persistence Order (strict)
1. `extraction_runs` insert
2. `ncd_studies` upsert (per study)
3. Insert NCD values (`ncd_noael`, `ncd_pk_parameters`, ...)
4. Insert trace link (`extracted_entities`) with anchor + confidence
5. Optional: low-confidence `document_comments`

## Step 7 — Validation & Review
Low confidence triggers an open comment:
```python
if confidence < 0.75:
    create_document_comment(anchor=anchor, content="⚠️ Low-confidence extraction. Please review.")
```

## Step 8 — LangChain Orchestration (LangGraph recommended)
Graph:
```
load_doc → segment_studies → for each study:
   ├─ extract_metadata
   ├─ extract_noael
   ├─ extract_pk
   └─ validate
→ persist_results
```

Example node:
```python
def extract_noael_node(state):
    results = []
    for chunk in state["study_chunks"]:
        res = noael_chain.invoke({"text": chunk.text})
        if res:
            results.append((res, chunk))
    return {"noael_results": results}
```

## Step 9 — Chat-ready Retrieval
```sql
SELECT n.*, e.anchor
FROM ncd_noael n
JOIN extracted_entities e
  ON e.entity_id = n.id
WHERE n.species = 'rat';
```

UI: highlight quote, jump to PDF, show reviewer comments.

---

## Reference Implementation (local runnable skeleton)
We ship a scaffolding orchestrator in `src/pdf_analysis/pipeline/langchain_extraction.py`:
- Runs study segmentation, NOAEL, PK chains
- Builds anchors from `DocumentChunk`s
- Computes composite confidence
- Persists to `extraction_runs`, `ncd_studies`, `ncd_noael`, `ncd_pk_parameters`, `extracted_entities`
- Auto-opens review comments for low-confidence items (<0.75)

Quick wiring example (assumes you provide the chains):
```python
from pathlib import Path
from pdf_analysis.pipeline.pipeline import PDFProcessingPipeline
from pdf_analysis.pipeline.langchain_extraction import LangChainExtractionPipeline
from pdf_analysis.pipeline.langchain_chains import (
    build_study_segmentation_chain,
    build_noael_chain,
    build_pk_chain,
)

study_segmentation_chain = build_study_segmentation_chain()
noael_chain = build_noael_chain()
pk_chain = build_pk_chain()

pdf_pipeline = PDFProcessingPipeline()
pdf_result = pdf_pipeline.run(Path("sample.pdf"))
chunks = [DocumentChunk(**c) for c in pdf_result.extras["document_chunks"]]

lc_pipeline = LangChainExtractionPipeline(
    study_segmentation_chain=study_segmentation_chain,
    noael_chain=noael_chain,
    pk_chain=pk_chain,
)

persisted = lc_pipeline.run(
    document_version_id="...uuid...",
    source_document_id="...uuid...",
    extractor_name="langchain_v1",
    model_name="gpt-4.1",
    chunks=chunks,
    created_by="...uuid...",
)
print(persisted)
```
