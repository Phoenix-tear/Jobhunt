-- 005_embeddings_and_usage: embedding cache metadata and the LLM usage ledger (step 3, phases C-D).
-- Additive only. `job_embeddings` already exists (migration 001, reserved); it gains the columns
-- that decide whether a stored vector is still valid.

-- A vector is reusable only when model, dimensions, prompt format and text hash all match.
ALTER TABLE job_embeddings ADD COLUMN dimensions     INTEGER;
ALTER TABLE job_embeddings ADD COLUMN prompt_version TEXT;
ALTER TABLE job_embeddings ADD COLUMN text_hash      TEXT;     -- hash of the exact text that was embedded
ALTER TABLE job_embeddings ADD COLUMN token_estimate INTEGER;

CREATE INDEX idx_job_embeddings_model ON job_embeddings (model, dimensions);

-- One vector per profile (resume) and model.
CREATE TABLE profile_embeddings (
    profile_name    TEXT    NOT NULL CHECK (profile_name IN ('ai', 'fullstack')),
    model           TEXT    NOT NULL,
    dimensions      INTEGER NOT NULL,
    prompt_version  TEXT    NOT NULL,
    text_hash       TEXT    NOT NULL,
    vector          BLOB    NOT NULL,
    created_at      TEXT    NOT NULL,
    PRIMARY KEY (profile_name, model)
);

-- Every paid call, so budgets can be enforced and spend explained. Never holds prompt text.
CREATE TABLE llm_usage (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT    NOT NULL,
    day             TEXT    NOT NULL,      -- UTC date, for the daily caps
    model           TEXT    NOT NULL,
    purpose         TEXT    NOT NULL,      -- embed | classify | score | remote
    calls           INTEGER NOT NULL DEFAULT 1,
    items           INTEGER NOT NULL DEFAULT 1,   -- texts embedded / jobs judged in this call
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,   -- includes thinking tokens (billed as output)
    estimated_usd   REAL    NOT NULL DEFAULT 0,
    note            TEXT
);

CREATE INDEX idx_llm_usage_day     ON llm_usage (day, purpose);
CREATE INDEX idx_llm_usage_created ON llm_usage (created_at);

-- A scored job is re-scored only when its inputs changed (description or prompt version).
ALTER TABLE job_relevance ADD COLUMN llm_input_hash TEXT;
