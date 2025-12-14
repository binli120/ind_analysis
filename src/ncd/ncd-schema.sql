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
                                           extra_attributes        JSONB DEFAULT '{}'::jsonb
);

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


