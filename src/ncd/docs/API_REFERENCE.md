# API Reference (NCD + Upload)

This summarizes the main FastAPI routes exposed by `pdf_analysis.api.server` that are relevant to Module 4 labeling and ingestion.

## Upload/S3 (router: upload)
- `POST /analyze` — Analyze a PDF from multipart upload or S3 JSON payload; returns markdown, tables, quality, metrics.
- `POST /s3/upload-analyze` — Upload PDF to S3, run analysis, and write sidecars (`.meta.json`, `.analysis.json`).
- `POST /s3/markdown` — Refresh markdown from S3 PDF and persist metadata.
- `POST /s3/markdown/summary` — Refresh markdown + summary/topics via OpenAI.
- `POST /s3/markdown/save` — Save edited markdown back to S3 with optional tags/metadata.
- `GET  /s3/analysis/status` — Check if async analysis JSON exists.
- `GET  /s3/analysis/result` — Fetch persisted analysis JSON.

## NCD (router: ncd)
- `POST /ncd/label` — S3-only lightweight section labeling. Inputs: `bucket` (or `S3_BUCKET` env), `key`, `company`, `project`, optional `page_limit` (default 5), `use_llm`. Reads minimal pages, classifies against `ind_24_26_template.json`, copies the PDF to `company/project/<section|unlabeled>/filename`, and updates S3 metadata + `.meta.json`.
- `POST /ncd/relabel` — Manually correct a section: provide `bucket` (or `S3_BUCKET`), `key`, `company`, `project`, `section_number`, optional `section_title`; copies the PDF into `company/project/<section>/filename`, deletes the original key, and updates metadata + `.meta.json` with `classification_method=manual`.

## Dev/QA (router: dev)
- `POST /dev/label-local` — Local upload labeling (no S3 side effects); returns section guess, confidence, method, and top candidates.
- `GET  /dev/sections` — List known template sections loaded from `src/ncd/ind_24_26_template.json`.

## Notes
- Template-driven labels come from `src/ncd/ind_24_26_template.json` (Modules 2.4/2.6).
- Text sampling is minimal (default first 5 pages) with pdfminer → OCR → PyMuPDF → pdfplumber fallbacks; filename hints are used when present (e.g., `4.2.1.1.pdf`).
- Optional LLM refinement requires `OPENAI_API_KEY` and infra extras installed.
- S3 routes require `boto3` (`poetry install --with infra`). Default bucket can be set via `S3_BUCKET`.
- Regenerate OpenAPI spec: `python - <<'PY'\nfrom pdf_analysis.api.server import app, export_openapi_to_file\nexport_openapi_to_file(app, 'openapi.json')\nPY`
