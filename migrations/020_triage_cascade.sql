-- Migration 020: Triage cascade (Tier 1 classifier, Tier 2 local LLM, Tier 3 Gemini). Additive only.
ALTER TABLE job_llm_verdicts ADD COLUMN source TEXT NOT NULL DEFAULT 'gemini';

CREATE TABLE IF NOT EXISTS job_cascade_decisions (
    id             INTEGER PRIMARY KEY,
    job_id         INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    head           TEXT    NOT NULL,                            -- seniority | location | eligibility
    tier           INTEGER NOT NULL,                            -- 0 (rules) | 1 (classifier) | 2 (local_llm) | 3 (gemini)
    action         TEXT    NOT NULL,                            -- maybe | hidden | info | pass | exclude
    confidence     REAL    NOT NULL DEFAULT 0.0,
    evidence       TEXT,
    mode           TEXT    NOT NULL DEFAULT 'shadow',           -- shadow | active
    model          TEXT,
    decided_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cascade_job ON job_cascade_decisions (job_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_cascade_stats ON job_cascade_decisions (decided_at, tier, head);
