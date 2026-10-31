-- 004_relevance_and_profiles: the matching layer's tables (step 3, phase B).
-- Additive only. Nothing is ever deleted by the gate: every exclusion keeps its reason and a
-- config change recomputes the verdicts (rules_hash), so exclusions are reversible.

-- One row per open job the gate has judged. Later phases fill the similarity / LLM / score
-- columns in place (embeddings: phase C, Gemini scoring: phase D).
CREATE TABLE job_relevance (
    job_id                INTEGER PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    computed_at           TEXT    NOT NULL,
    rules_hash            TEXT    NOT NULL,   -- config + gate version + month: a change recomputes
    input_hash            TEXT    NOT NULL,   -- the job fields the verdict depends on
    gate_status           TEXT    NOT NULL CHECK (gate_status IN ('target', 'maybe', 'excluded')),
    gate_rule             TEXT    NOT NULL,   -- machine code: hard_exclude_title, primary_stack, ...
    gate_reason           TEXT    NOT NULL,   -- human sentence shown next to a hidden job
    role_family           TEXT    NOT NULL DEFAULT 'other' CHECK (role_family IN ('ai_ml', 'fullstack', 'other')),
    family_detail         TEXT,               -- devops | data_engineering | mobile | salesforce | salesforce_ai | ...
    rank_adjust           REAL    NOT NULL DEFAULT 0,
    confidence            TEXT    NOT NULL DEFAULT 'high' CHECK (confidence IN ('high', 'medium', 'low')),
    required_years        INTEGER,            -- years of experience the description asks for, if parsed
    best_profile          TEXT    CHECK (best_profile IS NULL OR best_profile IN ('ai', 'fullstack')),
    similarity_ai         REAL,
    similarity_fullstack  REAL,
    similarity_percentile REAL,
    llm_score             REAL,
    llm_reason            TEXT,
    llm_flags             TEXT,               -- JSON
    llm_model             TEXT,
    llm_prompt_version    TEXT,
    llm_status            TEXT    CHECK (llm_status IS NULL OR llm_status IN ('scored', 'failed', 'classified')),
    scored_at             TEXT,
    final_score           REAL,
    score_components      TEXT                -- JSON: the blend inputs, so the UI can explain the rank
);

CREATE INDEX idx_job_relevance_status ON job_relevance (gate_status, role_family);
CREATE INDEX idx_job_relevance_score  ON job_relevance (gate_status, final_score);
CREATE INDEX idx_job_relevance_rule   ON job_relevance (gate_rule);

-- The resumes, as text that may leave this machine: PII-stripped, hashed (see `profile-build`).
CREATE TABLE profiles (
    name           TEXT PRIMARY KEY CHECK (name IN ('ai', 'fullstack')),
    source_file    TEXT NOT NULL,
    source_hash    TEXT NOT NULL,   -- hash of the extracted raw text (detects a changed resume)
    text_hash      TEXT NOT NULL,   -- hash of the stripped text (what embeddings are keyed on)
    stripped_text  TEXT NOT NULL,
    pii_removed    TEXT,            -- JSON: counts per category, never the values
    built_at       TEXT NOT NULL
);
