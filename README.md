# Project Vision

This project provides an end‑to‑end workflow for turning complex PDF study reports into editable, analysable artefacts. The aim is to let subject-matter experts pull the raw content into collaborative tooling (markdown or rich-text editors), review and modify tables, perform lightweight analytics, and roundtrip their changes back into auditable deliverables.

## Core Idea

- **Universal extraction** – capture both narrative text and embedded tables from pharmaceutical PDF reports.
- **Editable representations** – emit markdown and HTML so reviewers can iterate inside modern editors (e.g. tiptap) without touching the raw PDF.
- **Structured data access** – serialise tables to CSV/JSON and create Python-friendly data frames for downstream processing.
- **Data-quality guardrails** – automatically validate extracted metadata to highlight integrity, consistency, and completeness issues before analysts make decisions.

## Methodology & Workflow

1. **Ingest**
   - Text extraction relies on `pdfminer.six`, with OCR fallback hooks for image-heavy pages.
   - Table detection supports multiple engines (`pdfplumber`, `camelot`, `tabula`) so we can switch strategies based on document layout.
2. **Transform**
   - Combine page text and table previews into a structured markdown narrative.
   - Build a parallel HTML document styled for editor-friendly rendering (the output loads cleanly in tiptap or similar WYSIWYG components).
3. **Persist**
   - Store markdown, HTML, CSV, and JSON artefacts alongside the original PDF.
   - Preserve edited markdown drafts by regenerating a `*.modified.pdf` snapshot before overwriting extractions.
4. **Validate**
   - Inspect tables for heuristics such as generic headers, blank cells, and inconsistent page counts.
   - Extract key/value metadata (dates, identifiers) and cross-check them against expectations derived from the file name and declaration text.
   - Emit both JSON and markdown quality reports so teams can automate checks or read-friendly summaries.

## Reasoning Behind the Approach

- **Analyst-first editing** – markdown/HTML outputs mirror the original page order, keeping context intact while enabling inline edits.
- **Redundancy in table engines** – different reports vary wildly; supporting multiple extraction strategies improves recall.
- **Lightweight QA** – automated checks catch obvious anomalies (e.g. mismatched report number, malformed dates) before deeper analytics, reducing manual triage time.
- **Roundtrip fidelity** – saving modified drafts to separate PDFs honours regulatory requirements to keep originals untouched while capturing revision history.

## Current Capabilities

- CLI command: `poetry run python -m pdf_analysis.cli extract <PDF> -o <outdir> [--engine] [--ocr-fallback] [--max-pages]`
- Outputs:
  - `*.extracted.md` – markdown with interleaved tables.
  - `*.extracted.html` – HTML suitable for rich-text editors.
  - `*.pX.tY.csv/json` – machine-readable tables.
  - `*.quality.json/md` – data-quality diagnostics.
  - Optional `*.modified.pdf` if the analyst previously edited the markdown draft.
- API service: `poetry run uvicorn pdf_analysis.api.server:app --reload`
  - `POST /analyze` with a multipart `file` field (`pdf` engine optional parameters `engine`, `max_pages`, `ocr_fallback`)
  - Response includes page text, markdown, HTML, table rows, and quality findings.

## Next Steps

- Expand validation rules with document-specific business logic (e.g. numeric ranges, cross-table comparison).
- Integrate an interactive notebook or light web UI to offer “playground” analytics directly on extracted tables.
- Implement PDF regeneration that merges edited markdown back into a polished report layout.
- Add automated regression tests using a curated corpus of sample PDFs to protect against extraction regressions.

## Deploying to AWS (ECR + ECS)

- Build the container locally with `docker build -t pdf-analysis:latest .`; the image exposes `PORT=8000` and serves the FastAPI app via Uvicorn.
- Push and deploy using `scripts/deploy_ecr_ecs.sh`; export `AWS_ACCOUNT_ID`, `AWS_REGION`, `ECS_CLUSTER`, `ECS_SERVICE`, and optionally override `ECR_REPOSITORY`, `IMAGE_TAG`, or `DOCKER_PLATFORM`.
- The script logs in to ECR, creates the repository if missing, builds/pushes the image, and triggers an ECS service rollout (`--force-new-deployment`).
- Ensure your ECS task definition maps container port `8000` to your load balancer target group or service discovery endpoint.
- Grant the execution role `AmazonECSTaskExecutionRolePolicy` and ECR read permissions so tasks can pull the image.

## Adaptive Extraction Pipeline

- The orchestrator in `pdf_analysis.pipeline` runs text extraction, OCR, structured parsing, and optional LangChain post-processing with graceful fallbacks.
- Configure stages via `PipelineConfig`—pick engines for pure text (`pdfplumber`, `pymupdf`, `pdfminer`), choose OCR providers (`tesseract`, `textract`, `gcv`), and supply regexes or SDK callables for form/table extraction.
- Enable LLM summarisation by installing the `llm` extra and toggling `LangChainConfig(enabled=True, provider="openai", model="gpt-4o-mini")`; the pipeline will build prompts and invoke the configured chain.
- Optional extras: `poetry install --with ocr,pymupdf,llm` (add `cloud_ocr` when wiring Google Vision or Textract clients). OCR fallbacks that use pdf2image expect the Poppler binaries (`pdfinfo`, `pdftoppm`) on `PATH`.
- Logging: configure via `logging.basicConfig(level=logging.INFO)` (or DEBUG) before constructing the pipeline; the module logs each stage’s progress under `pdf_analysis.pipeline`.
- Quick start:
  ```python
  from pathlib import Path
  from pdf_analysis.pipeline import (
      PDFProcessingPipeline,
      PipelineConfig,
      TextExtractionConfig,
      OCRConfig,
      StructuredExtractionConfig,
      LangChainConfig,
  )

  config = PipelineConfig(
      text=TextExtractionConfig(engines=("pdfplumber", "pymupdf", "pdfminer"), keep_intermediate=True),
      ocr=OCRConfig(enable=True, strategy="tesseract", languages="eng"),
      structured=StructuredExtractionConfig(
          table_engines=("pdfplumber", "camelot"),
          key_value_patterns={"protocol_id": r"Protocol\s*#?\s*(\\w+)"},
      ),
      llm=LangChainConfig(enabled=False),
  )

  result = PDFProcessingPipeline(config=config).run(Path("./sample.pdf"))
  print(result.text_engine, len(result.pages), result.langchain_output)
  ```
