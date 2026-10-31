-- 013_automation_today_writing: v3. Additive only (no existing table or column is changed or dropped).

-- Daily automation: one row per scheduled task execution (status, duration, error; no secrets).
CREATE TABLE scheduled_runs (
    id               INTEGER PRIMARY KEY,
    task             TEXT    NOT NULL,                 -- ingest | ollama-ensure | match | ingest-email | discover
    trigger          TEXT    NOT NULL DEFAULT 'scheduler',   -- scheduler | manual
    started_at       TEXT    NOT NULL,
    finished_at      TEXT,
    status           TEXT    NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'ok', 'failed', 'skipped', 'timeout')),
    duration_seconds REAL,
    exit_code        INTEGER,
    detail           TEXT,                             -- e.g. "gmail auth expired", "on battery", "lock held"
    error            TEXT,
    log_file         TEXT
);
CREATE INDEX idx_scheduled_runs_task ON scheduled_runs (task, id);

-- Small key/value state (last visit to the Today page, Gmail auth state, ...).
CREATE TABLE app_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL
);

-- "Open apply URL" clicks: when I opened a posting (drives "disappeared since you opened it").
CREATE TABLE job_opens (
    job_id           INTEGER PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    first_opened_at  TEXT    NOT NULL,
    last_opened_at   TEXT    NOT NULL,
    open_count       INTEGER NOT NULL DEFAULT 1
);

-- Cover letters and answers: one row per generated version (nothing is overwritten).
CREATE TABLE generated_texts (
    id               INTEGER PRIMARY KEY,
    job_id           INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    kind             TEXT    NOT NULL CHECK (kind IN ('cover_letter', 'answer')),
    question         TEXT,                             -- answers only
    cache_key        TEXT    NOT NULL,                 -- job text hash + question + profile hash + model + prompt version
    version          INTEGER NOT NULL DEFAULT 1,       -- per (job, kind, question)
    text             TEXT    NOT NULL,
    cited_json       TEXT,                             -- fact ids the text cites
    validation_json  TEXT,                             -- {ok, violations: [...]}
    word_count       INTEGER NOT NULL DEFAULT 0,
    source           TEXT    NOT NULL,                 -- model | template | refusal
    model            TEXT,
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL    NOT NULL DEFAULT 0,
    message          TEXT,
    pdf_path         TEXT,
    created_at       TEXT    NOT NULL
);
CREATE INDEX idx_generated_job   ON generated_texts (job_id, kind, id);
CREATE INDEX idx_generated_cache ON generated_texts (cache_key);

-- Tracker additions: contact / recruiter fields (applied date, notes, follow-up and the attached resume exist).
ALTER TABLE applications ADD COLUMN contact_name  TEXT;
ALTER TABLE applications ADD COLUMN contact_email TEXT;
ALTER TABLE applications ADD COLUMN contact_phone TEXT;
