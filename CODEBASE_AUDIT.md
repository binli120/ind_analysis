# Codebase Operational Workflow — Full Audit

> **Project:** pdf-analysis (PDF Document Analysis & Extraction System for IND/NCD)
> **Author:** Bin Lee (blee@filynai.com) — filynai.com
> **Audit Date:** 2026-02-15
> **Python:** ≥ 3.11 | **Framework:** FastAPI + SQLAlchemy + LangChain

---

## 1. Audit Trail (Step-by-Step)

The system has **four primary entry-point families**. Each is traced below with its own audit trail.

---

### 1A. FastAPI Web Service Workflow (Primary HTTP Path)

| Step # | Action / Process | Entity (Function / Class) | Artifact (File Path) |
| :--- | :--- | :--- | :--- |
| A01 | FastAPI application bootstrap; CORS middleware registered | `app = FastAPI(...)`, `CORSMiddleware` | `src/pdf_analysis/api/server.py:135-147` |
| A02 | Global exception-logging middleware intercepts all requests | `log_unhandled_exceptions()` | `src/pdf_analysis/api/server.py:162-171` |
| A03 | **Health check** probe (GET /health) | `health_check()` | `src/pdf_analysis/api/server.py:150-152` |
| A04 | **Pydantic request validation** — incoming JSON deserialized and validated against typed models | `S3AnalyzeRequest`, `S3MarkdownRequest`, `NCDLabelRequest`, etc. | `src/pdf_analysis/api/server.py:213-319` |
| A05 | S3 client download — PDF/DOCX fetched from S3 into temp file | `boto3.client("s3").download_file()` | `src/pdf_analysis/api/server.py` (endpoint body) |
| A06 | Text extraction from PDF pages (pdfminer.six, with pdfplumber/pymupdf fallback) | `extract_pages_text()` | `src/pdf_analysis/ingest/pdf_text.py:66-90` |
| A07 | OCR fallback for empty pages (pytesseract + pdf2image) | `ocr_pages_if_needed()` | `src/pdf_analysis/ingest/ocr.py:26-64` |
| A08 | Table extraction (pdfplumber primary; camelot/tabula fallback) | `extract_tables_all()` | `src/pdf_analysis/ingest/tables.py` |
| A09 | **Table validation** — header keyword detection, generic-header check, blank-cell count, numeric-row scoring | `_row_has_keywords()`, `_is_generic_header()`, `_row_header_score()` | `src/pdf_analysis/ingest/tables.py:56-100` |
| A10 | Table manifest assembled for rendering | `_build_table_manifest()` | `src/pdf_analysis/api/server.py:193-210` |
| A11 | Markdown document generation (GFM tables embedded) | `build_markdown_document()` | `src/pdf_analysis/transform/markdown_writer.py:31-76` |
| A12 | HTML document generation | `build_html_document()` | `src/pdf_analysis/transform/markdown_writer.py:79+` |
| A13 | Quality report generation — issues, key-value cross-checks, metadata consistency | `generate_quality_report()` | `src/pdf_analysis/validate/quality.py:121-179` |
| A14 | Optional: AI metadata generation (labels, keywords, language via OpenAI gpt-4o-mini) | `OpenAIMetadataGenerator.__call__()` | `src/pdf_analysis/service/ai_metadata.py:44-76` |
| A15 | Optional: Document summary + topic anchors via LLM | `OpenAIDocumentSummarizer.__call__()` | `src/pdf_analysis/service/document_summarizer.py:44-80` |
| A16 | Optional: Section summary embedding generation (OpenAI embeddings API) | `_generate_embedding()` | `src/pdf_analysis/api/server.py:357-379` |
| A17 | JSON response serialized and returned to caller | FastAPI `JSONResponse` | `src/pdf_analysis/api/server.py` |

---

### 1B. CLI Extraction Workflow

| Step # | Action / Process | Entity (Function / Class) | Artifact (File Path) |
| :--- | :--- | :--- | :--- |
| B01 | CLI argument parsing (`extract` subcommand) | `main()` via `argparse` | `src/pdf_analysis/cli.py:26-55` |
| B02 | **Input validation** — check PDF file existence | `pdf.exists()` guard | `src/pdf_analysis/cli.py:62-64` |
| B03 | Text extraction per page (pdfminer, optional OCR) | `extract_pages_text()` | `src/pdf_analysis/ingest/pdf_text.py:66-90` |
| B04 | OCR fallback for empty pages | `ocr_pages_if_needed()` | `src/pdf_analysis/ingest/ocr.py:26-64` |
| B05 | Table extraction (engine selectable via `--engine`) | `extract_tables_all()` | `src/pdf_analysis/ingest/tables.py` |
| B06 | Table persistence — CSV + JSON per table | `save_tables()` | `src/pdf_analysis/export/persist.py:41-74` |
| B07 | **Column uniqueness enforcement** on DataFrames | `ensure_unique_columns()` | `src/pdf_analysis/export/persist.py:21-38` |
| B08 | Markdown document build (text + table previews) | `build_markdown_document()` | `src/pdf_analysis/transform/markdown_writer.py:31-76` |
| B09 | **Modified-draft detection** — if existing markdown differs, back up as PDF | `markdown_to_pdf()` | `src/pdf_analysis/export/pdf_writer.py:13-45` |
| B10 | Write `.extracted.md` to disk | `write_text()` | `src/pdf_analysis/export/persist.py:16-18` |
| B11 | HTML document build and write | `build_html_document()` → `write_text()` | `src/pdf_analysis/transform/markdown_writer.py:79+` / `src/pdf_analysis/export/persist.py:16` |
| B12 | Quality report generation (JSON + Markdown) | `generate_quality_report()` | `src/pdf_analysis/validate/quality.py:121-179` |
| B13 | Quality report persistence to disk | `write_text()` | `src/pdf_analysis/export/persist.py:16-18` |

---

### 1C. Pipeline Orchestrator Workflow (`PDFProcessingPipeline.run()`)

| Step # | Action / Process | Entity (Function / Class) | Artifact (File Path) |
| :--- | :--- | :--- | :--- |
| C01 | Pipeline instantiation with config | `PDFProcessingPipeline.__init__()` | `src/pdf_analysis/pipeline/pipeline.py:188-192` |
| C02 | `PipelineContext` created (mutable state bag) | `PipelineContext` dataclass | `src/pdf_analysis/pipeline/pipeline.py:87-102` |
| C03 | **Text extraction stage** — iterates configured engines with fallback | `_run_text_extraction()` | `src/pdf_analysis/pipeline/pipeline.py:432-451` |
| C04 | Engine dispatch — pdfminer → pdfplumber → pymupdf | `_extract_text_with_engine()` | `src/pdf_analysis/pipeline/pipeline.py:453-503` |
| C05 | **Text payload validation** — checks if any page has non-empty text | `_has_text_payload()` | `src/pdf_analysis/pipeline/pipeline.py:565-566` |
| C06 | OCR stage (conditional on `config.ocr.enable`) | `_run_ocr_stage()` | `src/pdf_analysis/pipeline/pipeline.py:208` |
| C07 | **Structured extraction stage** — tables via configured engine(s) | `_run_structured_stage()` | `src/pdf_analysis/pipeline/pipeline.py:211` |
| C08 | Subprocess isolation for camelot/tabula engines (spawn + timeout) | `_run_table_engine_subprocess()` | `src/pdf_analysis/pipeline/pipeline.py:62-84` |
| C09 | **Key-value pair extraction** from 2-column tables | `_run_key_value_stage()` | `src/pdf_analysis/pipeline/pipeline.py:217` |
| C10 | Editor artifact generation (Markdown + HTML) | `_build_editor_artifacts()` | `src/pdf_analysis/pipeline/pipeline.py:220` |
| C11 | Quality report generation | `_generate_quality()` | `src/pdf_analysis/pipeline/pipeline.py:221` |
| C12 | Optional: LangChain extraction stage (study segmentation, NOAEL, PK) | `_run_langchain_stage()` | `src/pdf_analysis/pipeline/pipeline.py:229` |
| C13 | Metrics computation (pages, text coverage, confidence) | `_compute_metrics()` | `src/pdf_analysis/pipeline/pipeline.py:241` |
| C14 | `PipelineResult` assembled and returned | `PipelineResult` dataclass | `src/pdf_analysis/pipeline/pipeline.py:117-132` |

---

### 1D. SQS Worker / Lambda Workflow (Serverless Path)

| Step # | Action / Process | Entity (Function / Class) | Artifact (File Path) |
| :--- | :--- | :--- | :--- |
| D01 | **S3 ObjectCreated trigger** — Lambda extracts bucket/key from event | `handler()` (S3→SQS bridge) | `lambda/s3_to_sqs.py:26-49` |
| D02 | **Input validation** — skip records missing bucket or key | Guard clause | `lambda/s3_to_sqs.py:37-39` |
| D03 | SQS message published with payload `{bucket, key, version_id, tenant_id}` | `sqs.send_message()` | `lambda/s3_to_sqs.py:47` |
| D04 | SQS worker Lambda receives event, iterates Records | `handler()` (SQS worker) | `src/pdf_analysis/sqs_worker.py:72-90` |
| D05 | Message JSON parsed; `process_message()` invoked | `process_message()` | `src/pdf_analysis/sqs_worker.py:93-100` |
| D06 | **Tenant validation** — `tenant_id` required or RuntimeError raised | Guard clause | `src/pdf_analysis/sqs_worker.py:107-110` |
| D07 | S3 file download to temp directory | `s3.download_file()` | `src/pdf_analysis/sqs_worker.py:131-135` |
| D08 | **Content integrity** — SHA-256 hash computed for deduplication | `sha256_file()` | `src/pdf_analysis/sqs_worker.py:137` |
| D09 | **Idempotency check** — fetch existing pipeline status; skip if completed | `repo.fetch_pipeline_status()` | `src/pdf_analysis/sqs_worker.py:144-163` |
| D10 | Pipeline status set to "processing" in DB | `repo.upsert_pipeline_status()` | `src/pdf_analysis/sqs_worker.py:166-173` |
| D11 | Ingestion status set to "processing" | `repo.upsert_ingestion_status()` | `src/pdf_analysis/sqs_worker.py:174-180` |
| D12 | File type inferred; appropriate pipeline built (PDF vs DOCX) | `_build_pipeline_for_type()` | `src/pdf_analysis/sqs_worker.py:182` |
| D13 | **Core extraction** — `PDFProcessingPipeline.run()` (see Trail 1C) | `pipeline.run()` | `src/pdf_analysis/sqs_worker.py:183` |
| D14 | Document chunks built for downstream indexing | `pipeline._build_document_chunks()` | `src/pdf_analysis/sqs_worker.py:190` |
| D15 | **DB persistence** — document + version records upserted | `repo.ensure_document_and_version()` | `src/pdf_analysis/sqs_worker.py:192-202` |
| D16 | Markdown artifact uploaded to S3 (`{key}.extracted.md`) | `s3.put_object()` | `src/pdf_analysis/sqs_worker.py:204-212` |
| D17 | Quality report uploaded to S3 (`{key}.quality.json`) | `s3.put_object()` | `src/pdf_analysis/sqs_worker.py:213-219` |
| D18 | Pipeline + ingestion status set to "completed" | `repo.upsert_pipeline_status()` | `src/pdf_analysis/sqs_worker.py:221-237` |
| D19 | **Context pipeline** — image/table asset extraction with LLM descriptions | `_build_document_assets()` | `src/pdf_analysis/sqs_worker.py:301-309` |
| D20 | Asset descriptions via LLM (image/table captions + keywords) | `describe_image_asset()`, `describe_table_asset()` | `src/ncd/extraction/content_extractor.py` |
| D21 | Assets persisted to DB | `repo.upsert_document_assets()` | `src/pdf_analysis/sqs_worker.py:310-317` |
| D22 | **Key section extraction** via LLM (section boundaries + types) | `extract_key_sections_from_pages()` | `src/ncd/extraction/content_extractor.py` |
| D23 | Key sections persisted to DB | `repo.replace_document_key_sections()` | `src/pdf_analysis/sqs_worker.py:358` |
| D24 | **LangChain extraction** — study segmentation, NOAEL, PK | `LangChainExtractionPipeline.run()` | `src/pdf_analysis/pipeline/langchain_extraction.py` |
| D25 | LangChain chains built (structured-output, gpt-4o-mini) | `build_study_segmentation_chain()`, `build_noael_chain()`, `build_pk_chain()` | `src/pdf_analysis/pipeline/langchain_chains.py:46-80` |
| D26 | Extraction results persisted to NCD tables | `NCDRepository` write methods | `src/database/db_interface.py` |

---

### 1E. S3 → Redis Sync Service Workflow

| Step # | Action / Process | Entity (Function / Class) | Artifact (File Path) |
| :--- | :--- | :--- | :--- |
| E01 | Sync service instantiation with `S3SyncConfig` | `S3RedisSyncService.__init__()` | `src/pdf_analysis/service/s3_sync.py:83-99` |
| E02 | S3 bucket scan — iterate latest document versions | `_iter_latest_documents()` | `src/pdf_analysis/service/s3_sync.py:113` |
| E03 | **Skip-check** — if markdown sidecar already exists, skip (idempotency) | `_should_skip_document()` | `src/pdf_analysis/service/s3_sync.py:120-128` |
| E04 | Document download and processing (PDF or DOCX pipeline) | `_process_document()` | `src/pdf_analysis/service/s3_sync.py:136` |
| E05 | Optional: AI metadata generation | `_metadata_generator()` | `src/pdf_analysis/service/s3_sync.py` |
| E06 | Optional: Document summarization + topic anchors | `OpenAIDocumentSummarizer.__call__()` | `src/pdf_analysis/service/document_summarizer.py:44` |
| E07 | Optional: Embedding generation + storage (Supabase) | `SupabaseEmbeddingStore` | `src/pdf_analysis/service/embedding_store.py` |
| E08 | Markdown persisted to Redis (HSET with version cache key) | `redis_client.hset()` | `src/pdf_analysis/service/s3_sync.py` |

---

## 2. ASCII Flowchart

### 2A. FastAPI / HTTP Request Flow

```
                              ┌──────────────────────┐
                              │   HTTP Client/User    │
                              └──────────┬───────────┘
                                         │
                                    [A01] │  POST /analyze
                                         │  POST /s3/markdown
                                         ▼
                              ┌──────────────────────┐
                              │  FastAPI app          │
                              │  + CORS Middleware    │─────── [A03] GET /health → {"ok"}
                              │  + Exception Logger   │
                              └──────────┬───────────┘
                                         │
                                    [A02] │  log_unhandled_exceptions()
                                         │
                                    [A04] │  Pydantic model validation
                                         ▼
                              ┌──────────────────────┐
                              │  S3 Download          │
                              │  (boto3)         [A05]│
                              └──────────┬───────────┘
                                         │
                    ┌────────────────────┼────────────────────┐
                    ▼                    ▼                    ▼
          ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
          │ Text Extraction │  │Table Extraction  │  │  OCR Fallback   │
          │ (pdfminer)      │  │(pdfplumber)      │  │  (tesseract)    │
          │           [A06] │  │           [A08]  │  │          [A07]  │
          └────────┬────────┘  └────────┬────────┘  └────────┬────────┘
                   │                    │ [A09]               │
                   │              Table Validation            │
                   │                    │                     │
                   └────────────────────┼─────────────────────┘
                                        │
                                   [A10] │  Build Table Manifest
                                        ▼
                    ┌────────────────────┼────────────────────┐
                    ▼                    ▼                    ▼
          ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
          │   Markdown      │  │     HTML        │  │ Quality Report  │
          │   Builder       │  │     Builder     │  │  Generator      │
          │          [A11]  │  │          [A12]  │  │          [A13]  │
          └────────┬────────┘  └────────┬────────┘  └────────┬────────┘
                   │                    │                     │
                   └────────────────────┼─────────────────────┘
                                        │
                    ┌───────────────┬────┴────┬───────────────┐
                    ▼               ▼         ▼               ▼
              ┌──────────┐  ┌──────────┐ ┌──────────┐  ┌──────────┐
              │AI Metadata│  │ Summary  │ │Embedding │  │ JSON     │
              │(OpenAI)  │  │(OpenAI)  │ │(OpenAI)  │  │ Response │
              │    [A14] │  │    [A15] │ │    [A16] │  │    [A17] │
              └──────────┘  └──────────┘ └──────────┘  └──────────┘
```

### 2B. CLI Extraction Flow

```
          ┌──────────────────┐
          │  $ ind-x extract │
          │  (argparse) [B01]│
          └────────┬─────────┘
                   │
              [B02]│  Validate file exists
                   ▼
          ┌──────────────────┐       ┌──────────────────┐
          │ extract_pages_   │──────▶│ ocr_pages_if_    │
          │ text()     [B03] │ empty │ needed()    [B04] │
          └────────┬─────────┘       └────────┬─────────┘
                   │                          │
                   ▼                          │
          ┌──────────────────┐                │
          │ extract_tables_  │◀───────────────┘
          │ all()      [B05] │
          └────────┬─────────┘
                   │
              [B06]│  save_tables() → CSV + JSON
              [B07]│  ensure_unique_columns()
                   ▼
          ┌──────────────────┐
          │ build_markdown_  │
          │ document() [B08] │
          └────────┬─────────┘
                   │
              [B09]│  Check for modified draft → markdown_to_pdf()
              [B10]│  write_text(.extracted.md)
              [B11]│  write_text(.extracted.html)
                   ▼
          ┌──────────────────┐
          │ generate_quality_│
          │ report()   [B12] │
          └────────┬─────────┘
                   │
              [B13]│  write_text(.quality.json + .quality.md)
                   ▼
               [ DONE ]
```

### 2C. Pipeline Orchestrator (`PDFProcessingPipeline.run()`)

```
          ┌───────────────────────────┐
          │ PDFProcessingPipeline     │
          │ .run(pdf_path)       [C01]│
          └────────────┬──────────────┘
                       │
                  [C02]│  Create PipelineContext
                       ▼
          ┌───────────────────────────┐
          │ _run_text_extraction()    │
          │ Engine loop:         [C03]│
          │  pdfminer → pdfplumber   │
          │  → pymupdf          [C04]│
          └────────────┬──────────────┘
                       │
                  [C05]│  _has_text_payload() check
                       ▼
          ┌───────────────────────────┐
          │ _run_ocr_stage()     [C06]│
          │ (if config.ocr.enable)    │
          └────────────┬──────────────┘
                       │
                       ▼
          ┌───────────────────────────┐
          │ _run_structured_stage()   │
          │ Table engines        [C07]│
          │  ├─ in-process (pdfplumber│
          │  └─ subprocess  [C08]     │
          │    (camelot/tabula)       │
          └────────────┬──────────────┘
                       │
                  [C09]│  _run_key_value_stage()
                       ▼
          ┌───────────────────────────┐
          │ _build_editor_artifacts() │
          │  ├─ Markdown         [C10]│
          │  └─ HTML                  │
          └────────────┬──────────────┘
                       │
                  [C11]│  _generate_quality()
                       ▼
          ┌───────────────────────────┐
          │ _run_langchain_stage()    │
          │ (optional)           [C12]│
          └────────────┬──────────────┘
                       │
                  [C13]│  _compute_metrics()
                       ▼
          ┌───────────────────────────┐
          │ Return PipelineResult     │
          │                      [C14]│
          └───────────────────────────┘
```

### 2D. SQS / Lambda Serverless Flow

```
    ┌───────────┐      ┌───────────────┐      ┌───────────────────┐
    │  S3 Bucket│─────▶│ Lambda: s3_to_│─────▶│  SQS Queue        │
    │ (upload)  │ [D01]│ sqs    [D02/03]│      │                   │
    └───────────┘      └───────────────┘      └─────────┬─────────┘
                                                        │
                                                   [D04]│  SQS → Lambda
                                                        ▼
                                              ┌───────────────────┐
                                              │ sqs_worker.       │
                                              │ handler()    [D05]│
                                              └─────────┬─────────┘
                                                        │
                                                   [D06]│  Validate tenant_id
                                                   [D07]│  S3 download
                                                   [D08]│  SHA-256 hash
                                                   [D09]│  Idempotency check
                                                        ▼
                                  ┌─────────────────────┼─────────────────────┐
                                  ▼                     ▼                     ▼
                        ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
                        │ CORE Pipeline   │   │ CONTEXT Pipeline│   │LANGCHAIN Pipeln.│
                        │ [D10-D18]       │   │ [D19-D23]       │   │ [D24-D26]       │
                        │                 │   │                 │   │                 │
                        │ ·Pipeline.run() │   │ ·Asset extract  │   │ ·Study segment  │
                        │ ·DB writes      │   │ ·LLM describe   │   │ ·NOAEL extract  │
                        │ ·S3 upload MD   │   │ ·Key sections   │   │ ·PK extract     │
                        │ ·Quality report │   │ ·DB persist     │   │ ·DB persist     │
                        └─────────────────┘   └─────────────────┘   └─────────────────┘
                                  │                     │                     │
                                  └─────────────────────┼─────────────────────┘
                                                        │
                                                        ▼
                                              ┌───────────────────┐
                                              │  PostgreSQL DB    │
                                              │  (NCD Schema)     │
                                              └───────────────────┘
```

---

## 3. Dependency & State Map

### 3.1 External Service Dependencies (Happy Path)

| # | Dependency | Required By | Protocol | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| 1 | **AWS S3** | API server, SQS worker, S3 sync | HTTPS (boto3) | Document storage & artifact persistence |
| 2 | **AWS SQS** | Lambda s3_to_sqs, sqs_worker | HTTPS (boto3) | Asynchronous job queue |
| 3 | **PostgreSQL** | SQS worker, API server (NCD endpoints) | TCP (psycopg2/SQLAlchemy) | Document metadata, NCD extraction results, pipeline status tracking |
| 4 | **Redis** | S3 sync service, pipeline streaming mode | TCP (redis-py) | Markdown cache, streaming chunk persistence |
| 5 | **OpenAI API** | AI metadata, document summarizer, LangChain chains, content extractor, embeddings | HTTPS (openai SDK) | LLM-powered classification, summarization, structured extraction |
| 6 | **Supabase** (optional) | Embedding store | HTTPS | Vector embedding storage for semantic search |

### 3.2 System-Level Dependencies

| # | Dependency | Required By | Purpose |
| :--- | :--- | :--- | :--- |
| 1 | **poppler-utils** | `pdf2image` (OCR path) | PDF → image rasterization |
| 2 | **Tesseract OCR** | `pytesseract` (OCR fallback) | Image → text recognition |
| 3 | **Ghostscript** | PDF operations (Docker) | PDF rendering and manipulation |
| 4 | **Java 21 JRE** | `tabula-py` table engine | Java-based table extraction |

### 3.3 Key Python Library Dependencies

| # | Library | Version | Purpose |
| :--- | :--- | :--- | :--- |
| 1 | `pdfminer.six` | ≥ 20221105 | Primary PDF text extraction engine |
| 2 | `pdfplumber` | ≥ 0.11.7 | Alternate text extraction + primary table extraction |
| 3 | `pymupdf (fitz)` | ≥ 1.26.6 | Tertiary text extraction fallback |
| 4 | `pandas` | ≥ 2.3.3 | DataFrame operations for tables |
| 5 | `fastapi` | ≥ 0.111.0 | HTTP API framework |
| 6 | `uvicorn` | ≥ 0.30.0 | ASGI web server |
| 7 | `pydantic` | ≥ 2.12.0 | Request/response model validation |
| 8 | `sqlalchemy` | ≥ 2.0.44 | ORM / database access |
| 9 | `openai` | ≥ 2.14.0 | LLM API client |
| 10 | `langchain-openai` | optional | LangChain structured extraction |
| 11 | `python-docx` | ≥ 1.1.2 | DOCX text and table parsing |
| 12 | `reportlab` | optional | PDF generation from markdown |
| 13 | `boto3` | optional (infra) | AWS SDK for S3/SQS |
| 14 | `redis` | optional (infra) | Redis client |
| 15 | `rapidfuzz` | — | Fuzzy string matching (study ID matching) |

### 3.4 Required Environment Variables (Happy Path)

| Variable | Required By | Purpose |
| :--- | :--- | :--- |
| `DATABASE_URL` | NCD database layer | PostgreSQL connection string |
| `OPENAI_API_KEY` | AI metadata, summarizer, LangChain, content extractor | LLM API authentication |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | S3/SQS operations | AWS authentication |
| `AWS_DEFAULT_REGION` | boto3 client | AWS region selection |
| `PIPELINE_QUEUE_URL` | Lambda s3_to_sqs | SQS queue URL for job dispatch |
| `DEFAULT_TENANT_ID` | SQS worker | Multi-tenant isolation |
| `DEFAULT_USER_ID` | SQS worker | Audit trail attribution |
| `PROJECT_ID` | SQS worker | Project scoping |
| `ENABLE_LANGCHAIN` | SQS worker | Feature flag (default: `true`) |
| `ENABLE_CONTEXT_PIPELINE` | SQS worker | Feature flag (default: `true`) |
| `LLM_MODEL` | SQS worker | LLM model override (default: `gpt-4o-mini`) |
| `REDIS_URL` | S3 sync service | Redis connection string |

### 3.5 Database State Requirements (Happy Path)

| Prerequisite | Table(s) | Condition |
| :--- | :--- | :--- |
| Alembic migrations applied | All NCD tables | Schema must be at latest revision |
| `ncd_source_document` rows exist | `ncd_source_document` | SQS worker creates on first ingestion |
| `documents` + `document_versions` exist | `documents`, `document_versions` | Created by `ensure_document_and_version()` |
| Pipeline status tracking | `document_ingestion_status` | Created/updated per pipeline run |
| Template entries loaded | `ctd_section_reference` | Required for NCD CTD mapping endpoints |

### 3.6 Validation Points Summary

| Location | Validation Type | Description |
| :--- | :--- | :--- |
| `server.py:213-319` | **Input sanitization** | Pydantic models validate all incoming JSON payloads; reject malformed requests |
| `server.py:162-171` | **Error handling** | Global middleware catches and logs unhandled exceptions |
| `sqs_worker.py:107-110` | **Authorization** | `tenant_id` required on every SQS message |
| `sqs_worker.py:137` | **Integrity** | SHA-256 content hash for deduplication and version tracking |
| `sqs_worker.py:144-163` | **Idempotency** | Skip processing if pipeline status is already "completed" |
| `tables.py:56-100` | **Data quality** | Table header keyword detection, generic-header detection, numeric-row scoring |
| `quality.py:121-179` | **Data quality** | Full quality report: empty tables, generic headers, blank cells, metadata cross-checks |
| `quality.py:76-110` | **Data transformation** | Key-value extraction from 2-column tables with colon-parsing heuristics |
| `persist.py:21-38` | **Data integrity** | Column name uniqueness enforcement before DataFrame serialization |
| `ai_metadata.py:27-41` | **Data cleaning** | Keyword deduplication and limit enforcement |
| `content_extractor.py:25-68` | **Text clipping** | Text length limits enforced before LLM calls; keyword cleaning and dedup |
| `pipeline.py:565-566` | **Extraction validation** | Verify non-empty text payload before proceeding |
| `pipeline.py:62-84` | **Process isolation** | Subprocess spawn + timeout for camelot/tabula (prevents memory leaks / hangs) |
| `ocr.py:32-33` | **Graceful degradation** | Check for optional OCR dependencies before attempting fallback |

---

*End of Audit*
