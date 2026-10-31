-- Tailored resumes: one row per tailoring run (cached by cache_key; nothing is deleted).
CREATE TABLE tailored_resumes (
    id               INTEGER PRIMARY KEY,
    job_id           INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    base_profile     TEXT    NOT NULL,                 -- ai | fullstack
    mode             TEXT    NOT NULL,                 -- select | rephrase | deterministic
    cache_key        TEXT    NOT NULL,                 -- job + description hash + facts/ticks hash + base + mode + model
    plan_json        TEXT,
    tex_path         TEXT,
    pdf_path         TEXT,
    validation_json  TEXT,                             -- {ok, violations: [{rule, offending, source}]}
    status           TEXT    NOT NULL,                 -- ok | fallback | failed
    model            TEXT,
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL    NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    message          TEXT,
    pages            INTEGER NOT NULL DEFAULT 0,
    dropped_json     TEXT,
    approved         INTEGER NOT NULL DEFAULT 0,       -- 1: I approved this version
    approved_at      TEXT
);
CREATE INDEX idx_tailored_job   ON tailored_resumes (job_id, id);
CREATE INDEX idx_tailored_cache ON tailored_resumes (cache_key);

-- the tailored version attached to an application when it moved to Applied
ALTER TABLE applications ADD COLUMN tailored_resume_id INTEGER REFERENCES tailored_resumes (id);
