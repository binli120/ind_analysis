@copyright longooc.com
@author: Bin Lee
@email: blee@longooc.com

Module 4 extraction helpers for building the 2.4/2.6 scaffolding.

Prereqs
- Ensure `ocrmypdf` and `pandoc` are installed and on PATH (used for OCR + DOCX generation).

Commands
- Single PDF (local): `python src/dictionary/pipeline.py /path/to/input.pdf /path/to/output_dir`
  - Writes `<pdf-name>.md`, `<pdf-name>.docx`, plus `images/` and `tables/` inside the output dir.
- Batch from S3: `python scripts/process_module4_pdfs.py --bucket YOUR_BUCKET --prefix "company/project/Module 4" --output-dir tmp/module4_extracted`
  - Add `--limit 5` to cap how many PDFs run; add `--force` to overwrite existing per-PDF folders.
- Ingest + DB extraction: use `ncd.pipeline_runner.run_pdf_ingest_and_extract(pdf_path, project_id, llm_client=...)` to:
  - create `ncd_source_document`, `ncd_document_page`, `ncd_text_chunk` (+ embeddings)
  - classify the study
  - run LLM-based tox/PK extraction (NOAEL, dose groups, Cmax/AUC) into DB
  - returns IDs and summaries for downstream use
- Demo CLI to ingest and extract into DB (uses DummyLLM by default):
  ```
  poetry run python scripts/run_ncd_pipeline_demo.py <pdf_path> --project-id <PROJECT_UUID> [--module "Module 4"] [--chunk-max-chars 1500] [--no-embed]
  ```
  - `--project-id` must exist in the `projects` table.
  - Requires DB/pgvector availability via `DATABASE_URL`.

Notes
- Each PDF gets its own folder named after the PDF stem under the chosen output dir.
- Outputs are ready to feed into the Module 2.4/2.6 dictionary builder. 
