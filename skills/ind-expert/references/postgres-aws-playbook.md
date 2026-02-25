# Postgres and AWS Playbook

## Environment
- Set:
  - `DATABASE_URL`
  - `AWS_REGION`
  - `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (or role-based auth)
  - `OPENAI_API_KEY` when LLM extraction is required

## Batch Ingestion Entry Point
- Run Module 4 ingestion with:
```bash
poetry run python scripts/ingest_module4_batch.py --bucket <bucket> --company <company> --project <project>
```
- Use project-specific flags from `README.md` when running `core`, `tox`, or `pharm-overview` modes.

## Fast DB Validation Queries
- Validate ingestion by pipeline:
```sql
SELECT pipeline, status, COUNT(*) AS rows
FROM ncd_ingestion_pipeline_status
GROUP BY pipeline, status
ORDER BY pipeline, status;
```
- Validate study extraction coverage:
```sql
SELECT s.id, s.sponsor_study_id, s.study_type,
       COUNT(DISTINCT em.id) AS exposure_rows,
       COUNT(DISTINCT f.id) AS finding_rows
FROM ncd_study s
LEFT JOIN ncd_exposure_metric em ON em.study_id = s.id
LEFT JOIN ncd_finding f ON f.study_id = s.id
GROUP BY s.id, s.sponsor_study_id, s.study_type
ORDER BY s.study_type, s.sponsor_study_id;
```
- Validate safety summary completeness:
```sql
SELECT s.sponsor_study_id, ss.noael_mg_per_kg, ss.loael_mg_per_kg, ss.limiting_organ
FROM ncd_study s
LEFT JOIN ncd_study_safety_summary ss ON ss.study_id = s.id
WHERE s.study_type ILIKE '%tox%';
```

## PK/Tox QA Rules
- Require non-empty `finding_term` for tox findings.
- Require numeric PK values where `parameter` is present.
- Require at least one source trace (`source_chunk_id` or equivalent) for key endpoints.

## AWS-S3 Operational Checks
- Confirm key availability:
```bash
aws s3 ls "s3://<bucket>/<company>/<project>/Module 4/" --recursive | head -n 20
```
- Inspect generated ingestion report:
```bash
ls -t tmp/ingestion_reports/ingestion_module4-*.json | head -n 1
```

## Validation Script
- Validate generated IND outputs:
```bash
poetry run python scripts/validate_ind24_outputs.py --gap-json <gap.json> --summary-json <summary.json>
```
