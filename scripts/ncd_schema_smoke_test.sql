-- Smoke test: populate the Supabase/NCD tables end-to-end.
-- Usage:
--   psql "$DATABASE_URL" -f scripts/ncd_schema_smoke_test.sql

BEGIN;

WITH constants AS (
    SELECT
        '00000000-0000-0000-0000-000000000001'::uuid AS tenant_id,
        '00000000-0000-0000-0000-0000000000aa'::uuid AS user_id,
        'demo-study-001'::text AS study_identifier,
        'demo-content-hash-1'::text AS content_hash,
        'demo-bucket'::text AS bucket,
        'demo/key.pdf'::text AS s3_key
),
doc AS (
    SELECT id FROM documents
    WHERE tenant_id = (SELECT tenant_id FROM constants)
      AND title = 'Demo Study Document'
    LIMIT 1
),
doc_ins AS (
    INSERT INTO documents (tenant_id, title)
    SELECT tenant_id, 'Demo Study Document'
    FROM constants
    WHERE NOT EXISTS (SELECT 1 FROM doc)
    RETURNING id
),
doc_final AS (
    SELECT id FROM doc_ins
    UNION ALL
    SELECT id FROM doc
),
doc_ver AS (
    SELECT id, document_id FROM document_versions
    WHERE document_id = (SELECT id FROM doc_final)
      AND content_hash = (SELECT content_hash FROM constants)
    LIMIT 1
),
doc_ver_ins AS (
    INSERT INTO document_versions (
        document_id, s3_bucket, s3_key, s3_version_id,
        file_type, content_hash, page_count, created_by
    )
    SELECT
        (SELECT id FROM doc_final),
        (SELECT bucket FROM constants),
        (SELECT s3_key FROM constants),
        'v1',
        'pdf',
        (SELECT content_hash FROM constants),
        42,
        (SELECT user_id FROM constants)
    WHERE NOT EXISTS (SELECT 1 FROM doc_ver)
    RETURNING id, document_id
),
doc_ver_final AS (
    SELECT id, document_id FROM doc_ver_ins
    UNION ALL
    SELECT id, document_id FROM doc_ver
),
run AS (
    SELECT id FROM extraction_runs
    WHERE document_version_id = (SELECT id FROM doc_ver_final)
    LIMIT 1
),
run_ins AS (
    INSERT INTO extraction_runs (
        document_version_id, module, extractor, model, status, created_by
    )
    SELECT
        (SELECT id FROM doc_ver_final),
        '4',
        'pk_llm_v3',
        'gpt-4.1',
        'completed',
        (SELECT user_id FROM constants)
    WHERE NOT EXISTS (SELECT 1 FROM run)
    RETURNING id
),
run_final AS (
    SELECT id FROM run_ins
    UNION ALL
    SELECT id FROM run
),
study AS (
    SELECT id FROM ncd_studies
    WHERE study_id = (SELECT study_identifier FROM constants)
),
study_ins AS (
    INSERT INTO ncd_studies (
        study_id, study_type, species, route, duration, source_document_id
    )
    SELECT
        (SELECT study_identifier FROM constants),
        'tox',
        'rat',
        'IV',
        '28-day',
        (SELECT document_id FROM doc_ver_final)
    WHERE NOT EXISTS (SELECT 1 FROM study)
    RETURNING id
),
study_final AS (
    SELECT id FROM study_ins
    UNION ALL
    SELECT id FROM study
),
noael AS (
    SELECT id FROM ncd_noael
    WHERE study_id = (SELECT id FROM study_final)
    LIMIT 1
),
noael_ins AS (
    INSERT INTO ncd_noael (
        study_id, dose, dose_unit, species, sex, endpoint, value
    )
    SELECT
        (SELECT id FROM study_final),
        50,
        'mg/kg',
        'rat',
        'male',
        'clinical observations',
        'No adverse effects at 50 mg/kg'
    WHERE NOT EXISTS (SELECT 1 FROM noael)
    RETURNING id
),
noael_final AS (
    SELECT id FROM noael_ins
    UNION ALL
    SELECT id FROM noael
),
pk AS (
    SELECT id FROM ncd_pk_parameters
    WHERE study_id = (SELECT id FROM study_final)
      AND parameter = 'Cmax'
),
pk_ins AS (
    INSERT INTO ncd_pk_parameters (
        study_id, parameter, value, unit, dose_group
    )
    SELECT
        (SELECT id FROM study_final),
        'Cmax',
        123.4,
        'ng/mL',
        'High'
    WHERE NOT EXISTS (SELECT 1 FROM pk)
    RETURNING id
),
pk_final AS (
    SELECT id FROM pk_ins
    UNION ALL
    SELECT id FROM pk
),
ent_noael AS (
    SELECT id FROM extracted_entities
    WHERE entity_type = 'NOAEL'
      AND entity_id = (SELECT id FROM noael_final)
),
ent_noael_ins AS (
    INSERT INTO extracted_entities (
        extraction_run_id, document_version_id,
        entity_type, entity_id, anchor, confidence
    )
    SELECT
        (SELECT id FROM run_final),
        (SELECT id FROM doc_ver_final),
        'NOAEL',
        (SELECT id FROM noael_final),
        '{"page":1,"quote":"No adverse effects at 50 mg/kg","bbox":[0.1,0.1,0.5,0.2]}'::jsonb,
        0.92
    WHERE NOT EXISTS (SELECT 1 FROM ent_noael)
    RETURNING id
),
ent_pk AS (
    SELECT id FROM extracted_entities
    WHERE entity_type = 'PK_PARAM'
      AND entity_id = (SELECT id FROM pk_final)
),
ent_pk_ins AS (
    INSERT INTO extracted_entities (
        extraction_run_id, document_version_id,
        entity_type, entity_id, anchor, confidence
    )
    SELECT
        (SELECT id FROM run_final),
        (SELECT id FROM doc_ver_final),
        'PK_PARAM',
        (SELECT id FROM pk_final),
        '{"page":2,"quote":"Cmax 123.4 ng/mL","bbox":[0.1,0.3,0.4,0.4]}'::jsonb,
        0.88
    WHERE NOT EXISTS (SELECT 1 FROM ent_pk)
    RETURNING id
),
val AS (
    SELECT id FROM ncd_validation
    WHERE entity_type = 'NOAEL'
      AND entity_id = (SELECT id FROM noael_final)
),
val_ins AS (
    INSERT INTO ncd_validation (
        entity_type, entity_id, status, reviewer_id, comment
    )
    SELECT
        'NOAEL',
        (SELECT id FROM noael_final),
        'accepted',
        (SELECT user_id FROM constants),
        'Seed data'
    WHERE NOT EXISTS (SELECT 1 FROM val)
    RETURNING id
)
SELECT 'seed-complete' AS status;

COMMIT;
