-- v8: LLM-assisted triage. Additive only; no existing row or column changes meaning.
--   job_llm_verdicts : one row per judged posting (cached by content_hash + rubric_version, so an unchanged posting is never judged
--                      twice). raw_json is what the model returned; verified_json is the same after the deterministic check that every
--                      evidence quote is an exact substring of the evidence extract (an unverifiable field is downgraded to unknown).
--                      human_label / human_reason: set when I press "LLM verdict is wrong"; such a job is exempt from the LLM rules.
--   job_relevance.triage_json : the student models' probabilities and the rules that demoted the job (chips only; never excludes).
CREATE TABLE job_llm_verdicts (
    id             INTEGER PRIMARY KEY,
    job_id         INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    content_hash   TEXT    NOT NULL,
    rubric_version TEXT    NOT NULL,
    model          TEXT    NOT NULL,
    raw_json       TEXT    NOT NULL,
    verified_json  TEXT    NOT NULL,
    hallucinated   INTEGER NOT NULL DEFAULT 0,        -- fields whose quote did not verify
    cost_usd       REAL    NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL,
    human_label    TEXT,                               -- wrong_level | wrong_years | wrong_location | other
    human_reason   TEXT,
    human_at       TEXT,
    UNIQUE (job_id, content_hash, rubric_version)
);
CREATE INDEX idx_verdicts_job   ON job_llm_verdicts (job_id, id);
CREATE INDEX idx_verdicts_cache ON job_llm_verdicts (content_hash, rubric_version);

ALTER TABLE job_relevance ADD COLUMN triage_json TEXT;
