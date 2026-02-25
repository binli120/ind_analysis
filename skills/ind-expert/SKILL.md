---
name: ind-expert
description: Project-specific FDA IND nonclinical expert for this codebase. Use when handling IND/eCTD Module 2.4 or 2.6 requests, interpreting FDA/ICH nonclinical requirements, ingesting Module 4 PDFs, extracting PK/tox/table evidence, validating data in PostgreSQL, mapping sections with section_List_mapping.json, or generating template-aligned summaries from ind_24_26_template.json.
---

# IND Expert

## Overview
- Execute end-to-end IND nonclinical workflows: ingestion, extraction, validation, and summary generation.
- Ground outputs in repository data and schema instead of generic IND advice.
- Produce evidence-traceable summaries for CTD 2.4/2.6 with explicit source anchors.

## Load Only Needed Context
- Read [references/project-context.md](references/project-context.md) first.
- Read [references/regulatory-baseline.md](references/regulatory-baseline.md) when the request is about requirements, gaps, or compliance rationale.
- Read [references/template-summary-workflow.md](references/template-summary-workflow.md) when generating section-level content.
- Read [references/postgres-aws-playbook.md](references/postgres-aws-playbook.md) when running ingestion, S3, or DB checks.
- Run `scripts/ind_context_lookup.py --section <section>` before writing a new section summary.
- Run `scripts/ind_context_lookup.py --section <section> --element <2.4.x-y>` when the request targets a single template element.

## Execution Tracks

### Regulatory Interpretation
- Map the question to FDA IND and ICH expectations using the regulatory baseline reference.
- Translate requirements into project evidence targets (which Module 4 reports, which DB tables, which CTD section).
- Flag uncertain interpretations as assumptions and request confirmation only when blocking.

### Ingestion or Extraction
- Use project ingestion paths and commands from the AWS/Postgres playbook.
- Confirm `ncd_source_document`, `ncd_document_page`, `ncd_text_chunk`, and extraction tables are populated.
- Extract PK and tox outputs with source traceability (`source_chunk_id`, page, section spans).

### Data Validation
- Validate expected fields for PK (`parameter`, `value`, `unit`) and tox (`finding_term`, severity, NOAEL/LOAEL).
- Validate cross-table integrity: studies -> dose groups -> exposure/findings -> summaries.
- Validate section mapping coverage before summary generation.

### Summary Generation
- Pull section constraints from template and mapping files with `scripts/ind_context_lookup.py`.
- Build the section narrative from evidence, not from assumptions.
- Keep each key claim tied to specific source evidence (study id, section, page/chunk).
- Generate concise, regulator-facing language and preserve units exactly.

## Output Contract
- Return section outputs with:
  - `section` and optional `element`.
  - `summary` text.
  - `critical_claims` list.
  - `evidence_map` list (module4 section, study id, DB/source anchor).
  - `data_gaps` list with required follow-up data.
- Prefer JSON when the user asks for machine-ingestable output; otherwise return markdown with the same fields.

## Non-Negotiable Quality Gates
- Do not invent data, study IDs, or numerical values.
- Preserve original units and identify conversions explicitly.
- Separate observed findings from interpretation.
- Call out missing evidence and unresolved mapping conflicts explicitly.
- Favor deterministic project scripts and SQL checks over free-form inference when validating.
