-- When did the local (GPU / Ollama) stages last run and how long did they take? Shown on the Health page.
CREATE TABLE local_runs (
    id         INTEGER PRIMARY KEY,
    stage      TEXT NOT NULL,          -- embed | ranker | local_llm
    started_at TEXT NOT NULL,
    seconds    REAL NOT NULL,
    items      INTEGER NOT NULL DEFAULT 0,
    model      TEXT,
    status     TEXT NOT NULL DEFAULT 'ok',   -- ok | skipped | failed
    detail     TEXT
);
CREATE INDEX idx_local_runs_stage ON local_runs (stage, id);

-- Gemini Batch API jobs (50% of the interactive price): submitted tonight, collected tomorrow.
CREATE TABLE llm_batches (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,         -- batches/<id> resource name
    model        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'failed')),
    jobs_json    TEXT NOT NULL,                -- [{key, job_id, input_hash, description_hash, profile_hash, ...}]
    applied_at   TEXT,
    detail       TEXT
);
