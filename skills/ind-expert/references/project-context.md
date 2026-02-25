# Project Context

## Core Inputs
- Template: `/Users/blee/dev/bin/pdf_analysis/src/ncd/ind_24_26_template.json`
- Module 4 -> 2.6 mapping: `/Users/blee/dev/bin/pdf_analysis/src/ncd/mapping/module4_to_26_mapping_complete.json`
- Section tree mapping: `/Users/blee/dev/bin/ind-manager/config/section_List_mapping.json`
- NCD schema: `/Users/blee/dev/bin/pdf_analysis/src/ncd/database/ncd-schema.sql`

## Key Code Paths
- Ingestion pipeline: `/Users/blee/dev/bin/pdf_analysis/src/ncd/pipeline/pipeline_runner.py`
- PDF ingestion primitives: `/Users/blee/dev/bin/pdf_analysis/src/ncd/ingestion/pdf_ingestion.py`
- Section detection: `/Users/blee/dev/bin/pdf_analysis/src/ncd/ingestion/section_detector.py`
- PK extractor: `/Users/blee/dev/bin/pdf_analysis/src/ncd/extraction/pk_extractor.py`
- Tox extractor: `/Users/blee/dev/bin/pdf_analysis/src/ncd/extraction/tox_extractor.py`
- Content extraction helpers: `/Users/blee/dev/bin/pdf_analysis/src/ncd/extraction/content_extractor.py`
- Batch Module 4 runner: `/Users/blee/dev/bin/pdf_analysis/scripts/ingest_module4_batch.py`
- IND 2.6 generator: `/Users/blee/dev/bin/pdf_analysis/scripts/generate_ind26.py`
- IND output validation: `/Users/blee/dev/bin/pdf_analysis/scripts/validate_ind24_outputs.py`

## NCD Tables to Use Most
- `ncd_source_document`: one row per ingested source PDF.
- `ncd_document_page`: extracted page text by page number.
- `ncd_text_chunk`: chunked text used for extraction and traceability.
- `ncd_study`: canonical study-level metadata.
- `ncd_dose_group`: tox dose cohorts.
- `ncd_exposure_metric`: PK/TK parameters (`Cmax`, `AUC`, `t1/2`, etc.).
- `ncd_finding`: structured tox findings.
- `ncd_study_safety_summary`: NOAEL/LOAEL and limiting findings.
- `ncd_ctd_section_reference`: evidence references tied to CTD sections.
- `ncd_ctd_section_summary`: generated written summaries.
- `ncd_ctd_tabulated_summary`: generated table-first summary payloads.
- `ncd_validation`: reviewer QA statuses.
- `ncd_ingestion_pipeline_status`: status by pipeline (`core`, `langchain`, `tox`, `section-summary`, `context`, `pharm-overview`).
- `ncd_ingestion_run_reports`: run-level batch reports.

## Existing Helpers
- Template loader/normalizer: `/Users/blee/dev/bin/pdf_analysis/src/ncd/config/ctd_template.py`
- CTD element resolver and evidence builder: `/Users/blee/dev/bin/pdf_analysis/src/ncd/types/ctd_elements.py`
- Module 4 mapping loader and target resolver: `/Users/blee/dev/bin/pdf_analysis/src/ncd/types/ctd_materials.py`

## Practical Rule
- Start with template + section mapping + Module 4 mapping before writing section content.
- Use DB evidence and section spans to support every high-impact claim.
