-- Allow the new `pharm-overview` pipeline status in existing databases.
-- Usage:
--   psql "$DATABASE_URL" -f scripts/fix_ingestion_pipeline_constraint.sql

DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'ncd_ingestion_pipeline_status'
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) ILIKE '%pipeline%'
    LOOP
        EXECUTE format(
            'ALTER TABLE ncd_ingestion_pipeline_status DROP CONSTRAINT IF EXISTS %I',
            r.conname
        );
    END LOOP;

    ALTER TABLE ncd_ingestion_pipeline_status
    ADD CONSTRAINT ncd_ingestion_pipeline_status_pipeline_check
    CHECK (
        pipeline IN (
            'core',
            'langchain',
            'tox',
            'section-summary',
            'context',
            'pharm-overview'
        )
    );
END $$;
