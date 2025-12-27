@copyright filynai.com
@author: Bin Lee
@email: blee@filynai.com

# Scripts Reference

Short descriptions for the helper scripts in this folder.

Core utilities
- `build.py`: Local CI helper (poetry install, mypy, pytest).
- `dev.sh`: Developer shortcut for build/run/lint/test via Poetry.
- `pipeline.py`: Run the PDF processing pipeline on a local PDF and write markdown/HTML/tables/quality outputs.

Ingestion + processing
- `ingest_module4_batch.py`: Batch ingest PDFs from S3 (core ingestion, section summaries, optional tox pipeline).
- `process_module4_pdfs.py`: Download PDFs from an S3 prefix and run the NCD pipeline into local output folders.
- `run_nightly_ingest.sh`: Example cron target for nightly ingestion (sets env vars, calls `ingest_module4_batch.py`).
- `s3_sync.py`: Sync S3 markdown sidecars into Redis; optional AI metadata, embeddings, and summaries.

NCD / LangChain demos
- `run_langchain_pipeline.py`: Run LangChain extraction on a local PDF (stub chains or DB-backed).
- `run_ncd_pipeline_demo.py`: Demo NCD ingest + extraction with a dummy LLM (writes to DB).

IND / CTD generation
- `generate_ind26_from_24.py`: Generate Section 2.6 outputs from local Section 2.4 content.
- `generate_ind26.py`: Load Section 2.4 markdown from S3, generate Section 2.6, write JSON/MD back to S3.
- `generate_ctd_template_docx.py`: Build DOCX templates from the 2.4/2.6 template JSON.
- `run_zero_shot_ind.py`: Zero-shot IND classification on a local PDF using OpenAI metadata labeling.
- `validate_ind24_outputs.py`: Validate Section 2.4 summary + gap analysis JSON (local or S3).

API + deployment
- `generate_openapi.py`: Export the FastAPI OpenAPI schema to `dist/openapi.json`.
- `common_aws_commands.sh`: Create an ALB and output Terraform variables.
- `deploy_ecr_ecs.sh`: Build/push Docker image and force ECS service deployment (uses env vars).
- `deploy_ecs.sh`: End-to-end ECR/ECS setup (repo, role, cluster, task def, service).
- `deploy_latest.sh`: Update an existing ECS service to a new image tag.

SQL + sample payloads
- `ncd_schema_smoke_test.sql`: Smoke test for populating core NCD tables via psql.
- `section_2_4_summary.json`: Sample Section 2.4 summary payload (generated example).
- `section_2_6_gap_analysis.json`: Sample gap analysis payload (generated example).
- `run-tests.sh`: Example one-liner for `s3_sync.py` (not a full test runner).
