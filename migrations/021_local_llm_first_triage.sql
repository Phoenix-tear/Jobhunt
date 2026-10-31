-- Migration 021: Local-LLM-first triage. Additive only.
-- 1. Create acting rules table (stores pass/fail evaluation of local LLM acting rules)
CREATE TABLE IF NOT EXISTS job_triage_acting_rules (
    id              INTEGER PRIMARY KEY,
    field           TEXT    NOT NULL,
    target_class    TEXT    NOT NULL,
    sample_count    INTEGER NOT NULL,
    high_conf_count INTEGER NOT NULL,
    precision_val   REAL    NOT NULL,
    recall_val      REAL    NOT NULL,
    can_act         INTEGER NOT NULL DEFAULT 0,
    mode            TEXT    NOT NULL DEFAULT 'advisory',
    reason          TEXT,
    evaluated_at    TEXT    NOT NULL,
    UNIQUE (field, target_class)
);

-- 2. Create disagreements table for high-stakes confirmation (pairs stored for training)
CREATE TABLE IF NOT EXISTS job_llm_disagreements (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    local_verdict   TEXT    NOT NULL,
    gemini_verdict  TEXT    NOT NULL,
    difference      TEXT    NOT NULL,
    resolved_by     TEXT    NOT NULL DEFAULT 'gemini',
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disagreements_job ON job_llm_disagreements (job_id);

-- 3. Upgrade job_llm_verdicts constraint to allow both gemini and local_llm verdicts per posting
CREATE TABLE IF NOT EXISTS job_llm_verdicts_v10 (
    id             INTEGER PRIMARY KEY,
    job_id         INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    content_hash   TEXT    NOT NULL,
    rubric_version TEXT    NOT NULL,
    model          TEXT    NOT NULL,
    raw_json       TEXT    NOT NULL,
    verified_json  TEXT    NOT NULL,
    hallucinated   INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL    NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL,
    human_label    TEXT,
    human_reason   TEXT,
    human_at       TEXT,
    source         TEXT    NOT NULL DEFAULT 'gemini',
    UNIQUE (job_id, content_hash, rubric_version, source)
);

INSERT OR IGNORE INTO job_llm_verdicts_v10 (
    id, job_id, content_hash, rubric_version, model, raw_json, verified_json,
    hallucinated, cost_usd, created_at, human_label, human_reason, human_at, source
)
SELECT id, job_id, content_hash, rubric_version, model, raw_json, verified_json,
       hallucinated, cost_usd, created_at, human_label, human_reason, human_at, source
FROM job_llm_verdicts;

DROP TABLE job_llm_verdicts;
ALTER TABLE job_llm_verdicts_v10 RENAME TO job_llm_verdicts;

CREATE INDEX IF NOT EXISTS idx_verdicts_job ON job_llm_verdicts (job_id, id);
CREATE INDEX IF NOT EXISTS idx_verdicts_cache ON job_llm_verdicts (content_hash, rubric_version, source);
