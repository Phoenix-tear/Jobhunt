-- 001_initial: core ingestion schema plus tables reserved for later steps.
-- Applied by jobhunt.db.apply_migrations (Flyway-style: ordered, recorded, checksummed).

-- ---------------------------------------------------------------------------
-- Companies: one row per (ATS, board slug). Drives which boards we poll.
-- ---------------------------------------------------------------------------
CREATE TABLE companies (
    id                      INTEGER PRIMARY KEY,
    name                    TEXT    NOT NULL,
    ats_type                TEXT    NOT NULL
        CHECK (ats_type IN ('greenhouse','lever','ashby','workable','smartrecruiters','recruitee','unknown')),
    slug                    TEXT    NOT NULL COLLATE NOCASE,
    region                  TEXT
        CHECK (region IS NULL OR region IN ('us','eu')),              -- Lever: which API host
    discovered_via          TEXT,                                      -- commoncrawl:<id> | snowball:<source> | seed | openroles | ...
    status                  TEXT    NOT NULL DEFAULT 'unverified'
        CHECK (status IN ('active','inactive','unverified')),
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    distinct_failure_days   INTEGER NOT NULL DEFAULT 0,
    consecutive_empty_runs  INTEGER NOT NULL DEFAULT 0,               -- guard against false mass-closure
    last_failure_at         TEXT,                                      -- lets us count distinct failure days
    last_checked_at         TEXT,
    last_ok_at              TEXT,
    last_error              TEXT,
    created_at              TEXT    NOT NULL,
    UNIQUE (ats_type, slug)
);

CREATE INDEX idx_companies_status_checked ON companies (status, last_checked_at);
CREATE INDEX idx_companies_ats_status     ON companies (ats_type, status);

-- ---------------------------------------------------------------------------
-- Jobs: one row per real-world posting. Several sightings may point at it.
-- ---------------------------------------------------------------------------
CREATE TABLE jobs (
    id                    INTEGER PRIMARY KEY,
    fingerprint           TEXT    NOT NULL,        -- sha256(normalized company | title | location)
    canonical_apply_url   TEXT,                    -- tracking params stripped, host aliases folded
    title                 TEXT    NOT NULL,
    company_id            INTEGER REFERENCES companies (id) ON DELETE SET NULL,
    company_name          TEXT    NOT NULL,
    location_raw          TEXT,
    country               TEXT,                    -- ISO 3166-1 alpha-2
    city                  TEXT,
    is_remote             INTEGER CHECK (is_remote IS NULL OR is_remote IN (0, 1)),
    employment_type       TEXT    NOT NULL DEFAULT 'unknown'
        CHECK (employment_type IN ('full_time','internship','contract','part_time','unknown')),
    experience_level      TEXT    NOT NULL DEFAULT 'unknown'
        CHECK (experience_level IN ('intern','new_grad','entry','mid','senior','unknown')),
    graduation_year_hint  INTEGER,
    description_text      TEXT,
    description_hash      TEXT,
    needs_description     INTEGER NOT NULL DEFAULT 0 CHECK (needs_description IN (0, 1)),
    posted_at             TEXT,
    first_seen_at         TEXT    NOT NULL,
    last_seen_at          TEXT    NOT NULL,
    missed_runs           INTEGER NOT NULL DEFAULT 0,
    closed_at             TEXT,
    status                TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    salary_min            REAL,                    -- annualized, see normalize.annualize_salary
    salary_max            REAL,
    salary_currency       TEXT,
    sponsorship_note      TEXT,
    raw_json              TEXT,                    -- kept only for jobs that pass the prefilter
    embedded_at           TEXT                     -- reserved for the embeddings step
);

CREATE INDEX idx_jobs_status_last_seen  ON jobs (status, last_seen_at);
CREATE INDEX idx_jobs_company           ON jobs (company_id);
CREATE INDEX idx_jobs_fingerprint       ON jobs (fingerprint);
CREATE INDEX idx_jobs_canonical_url     ON jobs (canonical_apply_url);
CREATE INDEX idx_jobs_status_country    ON jobs (status, country);
CREATE INDEX idx_jobs_status_experience ON jobs (status, experience_level);

-- ---------------------------------------------------------------------------
-- Sightings: where (and as which id) a job was seen. Idempotency lives here.
-- ---------------------------------------------------------------------------
CREATE TABLE job_sightings (
    id                INTEGER PRIMARY KEY,
    job_id            INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    source            TEXT    NOT NULL,
    source_job_id     TEXT    NOT NULL,
    source_url        TEXT,
    attribution_text  TEXT,
    first_seen_at     TEXT    NOT NULL,
    last_seen_at      TEXT    NOT NULL,
    UNIQUE (source, source_job_id)
);

CREATE INDEX idx_sightings_job            ON job_sightings (job_id);
CREATE INDEX idx_sightings_source_seen    ON job_sightings (source, last_seen_at);

-- ---------------------------------------------------------------------------
-- Operational tables
-- ---------------------------------------------------------------------------
CREATE TABLE ingestion_runs (
    id                         INTEGER PRIMARY KEY,
    source                     TEXT    NOT NULL,
    started_at                 TEXT    NOT NULL,
    finished_at                TEXT,
    companies_checked          INTEGER NOT NULL DEFAULT 0,
    jobs_seen                  INTEGER NOT NULL DEFAULT 0,
    jobs_new                   INTEGER NOT NULL DEFAULT 0,
    jobs_closed                INTEGER NOT NULL DEFAULT 0,
    jobs_skipped_by_prefilter  INTEGER NOT NULL DEFAULT 0,
    errors_json                TEXT
);

CREATE INDEX idx_runs_source_started ON ingestion_runs (source, started_at);

CREATE TABLE source_state (
    source             TEXT PRIMARY KEY,
    last_run_at        TEXT,
    last_success_at    TEXT,
    cursor_or_etag     TEXT,
    fetch_count_today  INTEGER NOT NULL DEFAULT 0,
    fetch_count_date   TEXT
);

CREATE TABLE processed_emails (
    gmail_message_id  TEXT PRIMARY KEY,
    processed_at      TEXT NOT NULL,
    sender            TEXT,
    parsed_job_count  INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Reserved for later steps (created now, intentionally left empty)
-- ---------------------------------------------------------------------------
CREATE TABLE job_embeddings (
    job_id            INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    model             TEXT    NOT NULL,
    vector            BLOB    NOT NULL,
    description_hash  TEXT,
    created_at        TEXT    NOT NULL,
    PRIMARY KEY (job_id, model)
);

CREATE TABLE master_resume (
    id            INTEGER PRIMARY KEY CHECK (id = 1),   -- single row
    content_json  TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE resume_versions (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    application_id  INTEGER REFERENCES applications (id) ON DELETE SET NULL,
    content_json    TEXT NOT NULL,
    latex_source    TEXT,
    pdf_path        TEXT
);

CREATE TABLE applications (
    id                 INTEGER PRIMARY KEY,
    job_id             INTEGER NOT NULL UNIQUE REFERENCES jobs (id) ON DELETE CASCADE,
    stage              TEXT NOT NULL DEFAULT 'saved'
        CHECK (stage IN ('saved','applied','oa_interview','offer','rejected')),
    applied_at         TEXT,
    resume_version_id  INTEGER REFERENCES resume_versions (id) ON DELETE SET NULL,
    notes              TEXT,
    follow_up_at       TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE application_stage_history (
    id              INTEGER PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications (id) ON DELETE CASCADE,
    from_stage      TEXT,
    to_stage        TEXT NOT NULL,
    changed_at      TEXT NOT NULL,
    note            TEXT
);

CREATE INDEX idx_stage_history_application ON application_stage_history (application_id, changed_at);
