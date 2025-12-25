CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

CREATE TABLE IF NOT EXISTS ncd_source_document (
                                                   id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                                                   project_id      UUID REFERENCES projects(id),
                                                   file_name       TEXT NOT NULL,
                                                   module          TEXT NOT NULL,
                                                   ctd_section     TEXT,
                                                   sha256          TEXT NOT NULL,
                                                   uploaded_at     TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ncd_document_page (
                                                 id                  BIGSERIAL PRIMARY KEY,
                                                 source_document_id  UUID REFERENCES ncd_source_document(id),
                                                 page_number         INT NOT NULL,
                                                 text                TEXT
);

CREATE TABLE IF NOT EXISTS ncd_text_chunk (
                                              id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                                              source_document_id  UUID REFERENCES ncd_source_document(id),
                                              study_id            UUID,
                                              section_label       TEXT,
                                              page_from           INT,
                                              page_to             INT,
                                              offset_start        INT,
                                              offset_end          INT,
                                              raw_text            TEXT NOT NULL
);

DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_indexes
            WHERE indexname = 'idx_ncd_text_chunk_fts'
        ) THEN
            CREATE INDEX idx_ncd_text_chunk_fts
                ON ncd_text_chunk
                    USING GIN (to_tsvector('english', raw_text));
        END IF;
    END $$;


CREATE TABLE IF NOT EXISTS ncd_text_chunk_embedding (
                                                        chunk_id   UUID PRIMARY KEY REFERENCES ncd_text_chunk(id),
                                                        embedding  VECTOR(1536)
);

CREATE TABLE IF NOT EXISTS ncd_study (
                                         id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                                         project_id       UUID REFERENCES projects(id),
                                         sponsor_study_id TEXT,
                                         study_type       TEXT NOT NULL,
                                         glp_status       TEXT,
                                         species          TEXT,
                                         strain           TEXT,
                                         route            TEXT,
                                         duration_days    INT,
                                         main_source_document_id UUID REFERENCES ncd_source_document(id),
                                         module4_section  TEXT,
                                         extra_attributes JSONB DEFAULT '{}'::jsonb,
                                         created_at       TIMESTAMPTZ DEFAULT now(),
                                         updated_at       TIMESTAMPTZ DEFAULT now()
);

DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_indexes
            WHERE indexname = 'idx_ncd_study_extra_gin'
        ) THEN
            CREATE INDEX idx_ncd_study_extra_gin
                ON ncd_study USING GIN (extra_attributes);
        END IF;
    END $$;

CREATE TABLE IF NOT EXISTS ncd_dose_group (
                                              id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                                              study_id          UUID REFERENCES ncd_study(id),
                                              name              TEXT,
                                              sex               TEXT,
                                              n_animals         INT,
                                              dose_mg_per_kg    NUMERIC,
                                              dose_mg_per_m2    NUMERIC,
                                              extra_attributes  JSONB DEFAULT '{}'::jsonb
);


CREATE TABLE IF NOT EXISTS ncd_exposure_metric (
                                                   id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                                                   study_id          UUID REFERENCES ncd_study(id),
                                                   dose_group_id     UUID REFERENCES ncd_dose_group(id),
                                                   species           TEXT,
                                                   matrix            TEXT,
                                                   parameter         TEXT,
                                                   value             NUMERIC,
                                                   unit              TEXT,
                                                   timepoint         TEXT,
                                                   clinical_multiple NUMERIC,
                                                   source_chunk_id   UUID REFERENCES ncd_text_chunk(id),
                                                   extra_attributes  JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS ncd_finding (
                                           id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                                           study_id                UUID REFERENCES ncd_study(id),
                                           organ_system            TEXT,
                                           organ                   TEXT,
                                           finding_term            TEXT,
                                           severity                TEXT,
                                           adverse                 BOOLEAN,
                                           reversible              BOOLEAN,
                                           onset_day               INT,
                                           recovery                TEXT,
                                           dose_threshold_mg_per_kg NUMERIC,
                                           noael_flag              BOOLEAN,
                                           source_chunk_id         UUID REFERENCES ncd_text_chunk(id),
                                           context_text            TEXT,
                                           context_page            INT,
                                           context_asset_ids       UUID[],
                                           is_positive             BOOLEAN,
                                           extra_attributes        JSONB DEFAULT '{}'::jsonb
);

ALTER TABLE ncd_finding
    ADD COLUMN IF NOT EXISTS context_text TEXT,
    ADD COLUMN IF NOT EXISTS context_page INT,
    ADD COLUMN IF NOT EXISTS context_asset_ids UUID[],
    ADD COLUMN IF NOT EXISTS is_positive BOOLEAN;

CREATE TABLE IF NOT EXISTS ncd_study_safety_summary (
                                                        id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                                                        study_id           UUID REFERENCES ncd_study(id),
                                                        noael_mg_per_kg    NUMERIC,
                                                        loael_mg_per_kg    NUMERIC,
                                                        limiting_organ     TEXT,
                                                        limiting_finding   TEXT,
                                                        clinical_multiple  NUMERIC,
                                                        source_chunk_id    UUID REFERENCES ncd_text_chunk(id)
);


CREATE TABLE IF NOT EXISTS ncd_topic (
                                         id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                                         name        TEXT NOT NULL,
                                         category    TEXT,
                                         parent_id   UUID REFERENCES ncd_topic(id)
);


CREATE TABLE IF NOT EXISTS ncd_topic_synonym (
                                                 id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                                                 topic_id    UUID REFERENCES ncd_topic(id),
                                                 synonym     TEXT
);


CREATE TABLE IF NOT EXISTS ncd_topic_synonym (
                                                 id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                                                 topic_id    UUID REFERENCES ncd_topic(id),
                                                 synonym     TEXT
);


CREATE TABLE IF NOT EXISTS ncd_topic_assignment (
                                                    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                                                    topic_id        UUID REFERENCES ncd_topic(id),
                                                    project_id      UUID REFERENCES projects(id),
                                                    study_id        UUID REFERENCES ncd_study(id),
                                                    finding_id      UUID REFERENCES ncd_finding(id),
                                                    exposure_id     UUID REFERENCES ncd_exposure_metric(id),
                                                    chunk_id        UUID REFERENCES ncd_text_chunk(id)
);


CREATE TABLE IF NOT EXISTS ncd_ctd_section_def (
                                                   code            TEXT PRIMARY KEY,
                                                   module          TEXT,
                                                   title           TEXT,
                                                   parent_code     TEXT REFERENCES ncd_ctd_section_def(code),
                                                   level           INT
);

CREATE TABLE IF NOT EXISTS ncd_ctd_section_def (
                                                   code            TEXT PRIMARY KEY,
                                                   module          TEXT,
                                                   title           TEXT,
                                                   parent_code     TEXT REFERENCES ncd_ctd_section_def(code),
                                                   level           INT
);


DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ncd_config_scope') THEN
            CREATE TYPE ncd_config_scope AS ENUM ('system', 'tenant', 'user', 'project');
        END IF;
    END $$;


CREATE TABLE IF NOT EXISTS ncd_config (
                                          id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                                          scope           ncd_config_scope NOT NULL,
                                          tenant_id       UUID REFERENCES tenants(id),
                                          user_id         UUID REFERENCES users(id),
                                          project_id      UUID REFERENCES projects(id),
                                          section_code    TEXT,
                                          config_key      TEXT NOT NULL,
                                          value           JSONB NOT NULL,
                                          version         INT DEFAULT 1,
                                          is_active       BOOLEAN DEFAULT TRUE,
                                          created_at      TIMESTAMPTZ DEFAULT now(),
                                          updated_at      TIMESTAMPTZ DEFAULT now()
);

-- Ensure extension for UUID generation is available
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS ncd_template_override (
                                                     id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

                                                     user_id UUID NOT NULL,
                                                     section TEXT NOT NULL,
                                                     subsection TEXT,

                                                     payload JSONB NOT NULL,

                                                     created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                                                     updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

                                                     CONSTRAINT ncd_template_override_user_section_subsection_unique
                                                         UNIQUE (user_id, section, subsection)
);

-- ============================================================
-- ENUMS
-- ============================================================

DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'comment_status') THEN
            CREATE TYPE comment_status AS ENUM ('open', 'resolved');
        END IF;
    END $$;

-- ============================================================
-- DOCUMENT LAYER
-- ============================================================

-- Logical document (e.g. "Module 4 – Repeat Dose Toxicity Study")
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    tenant_id UUID NOT NULL,
    title TEXT NOT NULL,

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Physical versions (S3-backed, hash-protected)
CREATE TABLE IF NOT EXISTS document_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    document_id UUID NOT NULL
        REFERENCES documents(id) ON DELETE CASCADE,

    s3_bucket TEXT NOT NULL,
    s3_key TEXT NOT NULL,
    s3_version_id TEXT NULL,

    file_type TEXT NOT NULL CHECK (file_type IN ('pdf','docx','txt','html')),
    content_hash TEXT NOT NULL,

    page_count INT NULL,

    created_by UUID NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE (document_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_document_versions_document
    ON document_versions(document_id);

-- ============================================================
-- DOCUMENT ASSETS (TABLES / IMAGES)
-- ============================================================

CREATE TABLE IF NOT EXISTS document_assets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    document_version_id UUID NOT NULL
        REFERENCES document_versions(id) ON DELETE CASCADE,

    asset_type TEXT NOT NULL CHECK (asset_type IN ('table', 'image')),
    page_number INT NULL,
    index_on_page INT NULL,

    s3_bucket TEXT NOT NULL,
    s3_key TEXT NOT NULL,

    caption TEXT NULL,
    description TEXT NULL,
    keywords TEXT[] NOT NULL DEFAULT '{}',
    extra_attributes JSONB DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE (document_version_id, asset_type, page_number, index_on_page, s3_key)
);

CREATE INDEX IF NOT EXISTS idx_document_assets_document
    ON document_assets(document_version_id);

CREATE INDEX IF NOT EXISTS idx_document_assets_type
    ON document_assets(asset_type);

CREATE INDEX IF NOT EXISTS idx_document_assets_keywords
    ON document_assets USING GIN (keywords);

-- ============================================================
-- SUMMARY / CONCLUSION PASSAGES
-- ============================================================

CREATE TABLE IF NOT EXISTS document_key_sections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    document_version_id UUID NOT NULL
        REFERENCES document_versions(id) ON DELETE CASCADE,

    section_type TEXT NOT NULL CHECK (section_type IN ('summary', 'conclusion')),

    text TEXT NOT NULL,

    page_start INT NULL,
    page_end INT NULL,
    char_start INT NULL,
    char_end INT NULL,

    asset_ids UUID[] NOT NULL DEFAULT '{}',

    model_name TEXT NULL,
    confidence FLOAT NULL,

    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_document_key_sections_document
    ON document_key_sections(document_version_id);

-- ============================================================
-- CTD 2.4 ELEMENT REFERENCES (CACHE)
-- ============================================================

CREATE TABLE IF NOT EXISTS ncd_ctd_section_reference (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    tenant_id UUID NOT NULL
        REFERENCES tenants(id),

    project_id UUID NOT NULL
        REFERENCES projects(id),

    bucket TEXT NOT NULL,

    element_number TEXT NOT NULL,
    section_number TEXT NULL,

    template_payload JSONB NOT NULL,
    module4_sections TEXT[] NOT NULL DEFAULT '{}',
    payload JSONB NOT NULL,

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE (tenant_id, project_id, bucket, element_number)
);

CREATE INDEX IF NOT EXISTS idx_ncd_ctd_section_reference_element
    ON ncd_ctd_section_reference(element_number);

CREATE INDEX IF NOT EXISTS idx_ncd_ctd_section_reference_project
    ON ncd_ctd_section_reference(project_id);

-- ============================================================
-- CTD SECTION SUMMARY DRAFTS + APPROVALS
-- ============================================================

CREATE TABLE IF NOT EXISTS ncd_ctd_section_summary (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    tenant_id UUID NOT NULL
        REFERENCES tenants(id),

    project_id UUID NOT NULL
        REFERENCES projects(id),

    bucket TEXT NOT NULL,
    section_number TEXT NOT NULL,
    element_numbers TEXT[] NOT NULL DEFAULT '{}',

    summary_text TEXT NOT NULL,
    final_text TEXT NULL,

    status TEXT NOT NULL CHECK (status IN ('draft', 'approved')),

    user_prompt TEXT NULL,
    user_comment TEXT NULL,
    previous_id UUID NULL
        REFERENCES ncd_ctd_section_summary(id),

    model_name TEXT NULL,
    embedding VECTOR(1536),

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ncd_ctd_section_summary_section
    ON ncd_ctd_section_summary(tenant_id, project_id, bucket, section_number);

CREATE INDEX IF NOT EXISTS idx_ncd_ctd_section_summary_status
    ON ncd_ctd_section_summary(status);

-- ============================================================
-- DOCUMENT COMMENTS (THREADED)
-- ============================================================

CREATE TABLE IF NOT EXISTS document_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    document_version_id UUID NOT NULL
        REFERENCES document_versions(id) ON DELETE CASCADE,

    parent_id UUID NULL
        REFERENCES document_comments(id) ON DELETE CASCADE,

    status comment_status DEFAULT 'open',

    anchor JSONB NOT NULL,     -- page, offsets, quote, bbox
    content TEXT NOT NULL,

    created_by UUID NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),

    resolved_by UUID NULL,
    resolved_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_comments_document_version
    ON document_comments(document_version_id);

CREATE INDEX IF NOT EXISTS idx_comments_parent
    ON document_comments(parent_id);

CREATE INDEX IF NOT EXISTS idx_comments_status
    ON document_comments(status);

-- ============================================================
-- EXTRACTION LAYER (LLM / RULE-BASED)
-- ============================================================

CREATE TABLE IF NOT EXISTS extraction_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    document_version_id UUID NOT NULL
        REFERENCES document_versions(id) ON DELETE CASCADE,

    module TEXT NOT NULL CHECK (module = '4'),

    extractor TEXT NOT NULL,        -- e.g. pk_llm_v3
    model TEXT NOT NULL,            -- gpt-4.1 / llama3 / etc

    status TEXT CHECK (status IN ('pending','completed','failed')),

    created_by UUID NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_extraction_runs_doc_version
    ON extraction_runs(document_version_id);

-- ============================================================
-- NCD CORE (STUDY LEVEL)
-- ============================================================

CREATE TABLE IF NOT EXISTS ncd_studies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    study_id TEXT NOT NULL,
    study_type TEXT NOT NULL CHECK (
        study_type IN ('tox','pk','safety_pharm','genotox','repro','carcinogenicity')
    ),

    species TEXT,
    route TEXT,
    duration TEXT,

    source_document_id UUID NOT NULL
        REFERENCES documents(id),

    created_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE (study_id)
);

-- ============================================================
-- NCD: NOAEL
-- ============================================================

CREATE TABLE IF NOT EXISTS ncd_noael (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    study_id UUID NOT NULL
        REFERENCES ncd_studies(id) ON DELETE CASCADE,

    dose NUMERIC,
    dose_unit TEXT,

    species TEXT,
    sex TEXT,

    endpoint TEXT,
    value TEXT,

    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ncd_noael_study
    ON ncd_noael(study_id);

-- ============================================================
-- NCD: PK PARAMETERS
-- ============================================================

CREATE TABLE IF NOT EXISTS ncd_pk_parameters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    study_id UUID NOT NULL
        REFERENCES ncd_studies(id) ON DELETE CASCADE,

    parameter TEXT NOT NULL CHECK (
        parameter IN ('AUC','Cmax','Tmax','t1/2','CL','Vd')
    ),

    value NUMERIC,
    unit TEXT,
    dose_group TEXT,

    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ncd_pk_study
    ON ncd_pk_parameters(study_id);

-- ============================================================
-- UNIVERSAL TRACEABILITY (DOCUMENT → NCD)
-- ============================================================

CREATE TABLE IF NOT EXISTS extracted_entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    extraction_run_id UUID NOT NULL
        REFERENCES extraction_runs(id) ON DELETE CASCADE,

    document_version_id UUID NOT NULL
        REFERENCES document_versions(id) ON DELETE CASCADE,

    entity_type TEXT NOT NULL,   -- 'NOAEL', 'PK_PARAM'
    entity_id UUID NOT NULL,     -- FK into ncd_* tables

    anchor JSONB NOT NULL,       -- page, offsets, quote
    confidence NUMERIC(4,3),

    created_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE (entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_extracted_entities_doc_version
    ON extracted_entities(document_version_id);

CREATE INDEX IF NOT EXISTS idx_extracted_entities_entity
    ON extracted_entities(entity_type, entity_id);

-- ============================================================
-- OPTIONAL: NCD VALIDATION (REVIEWER QA)
-- ============================================================

CREATE TABLE IF NOT EXISTS ncd_validation (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,

    status TEXT CHECK (status IN ('accepted','rejected','needs_review')),
    reviewer_id UUID NOT NULL,
    comment TEXT,

    created_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE (entity_type, entity_id)
);

-- ============================================================
-- INGESTION STATUS TRACKING
-- ============================================================

CREATE TABLE IF NOT EXISTS document_ingestion_status (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    s3_bucket TEXT NOT NULL,
    s3_key TEXT NOT NULL,
    s3_version_id TEXT NULL,

    content_hash TEXT NOT NULL,

    document_version_id UUID NULL
        REFERENCES document_versions(id) ON DELETE SET NULL,

    status TEXT NOT NULL CHECK (
        status IN ('pending','processing','completed','failed','skipped')
    ),

    error_message TEXT NULL,

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE (s3_bucket, s3_key, content_hash)
);

CREATE TABLE IF NOT EXISTS ncd_ingestion_pipeline_status (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    s3_bucket TEXT NOT NULL,
    s3_key TEXT NOT NULL,
    s3_version_id TEXT NULL,

    content_hash TEXT NOT NULL,
    pipeline TEXT NOT NULL CHECK (pipeline IN ('core','langchain','tox','section-summary','context')),
    status TEXT NOT NULL CHECK (
        status IN ('pending','processing','completed','failed','skipped')
    ),

    document_version_id UUID NULL
        REFERENCES document_versions(id) ON DELETE SET NULL,
    source_document_id UUID NULL
        REFERENCES ncd_source_document(id) ON DELETE SET NULL,
    study_id UUID NULL
        REFERENCES ncd_study(id) ON DELETE SET NULL,

    error_message TEXT NULL,

    started_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE (s3_bucket, s3_key, content_hash, pipeline)
);

CREATE TABLE IF NOT EXISTS ncd_ingestion_run_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    run_id TEXT NOT NULL UNIQUE,

    bucket TEXT NOT NULL,
    company TEXT,
    project TEXT,
    project_id UUID NULL,
    module INT,
    mode TEXT,
    core_check TEXT,
    md_suffix TEXT,
    force_core BOOLEAN DEFAULT FALSE,
    force_tox BOOLEAN DEFAULT FALSE,

    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,

    doc_processed INT DEFAULT 0,
    doc_failed INT DEFAULT 0,
    doc_skipped INT DEFAULT 0,
    core_runs INT DEFAULT 0,
    core_failed INT DEFAULT 0,
    tox_runs INT DEFAULT 0,
    tox_failed INT DEFAULT 0,

    error_summary JSONB,
    report_json JSONB,

    created_at TIMESTAMPTZ DEFAULT now()
);
