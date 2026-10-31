-- Local tiers: the ranker's predicted fit and the local LLM's verdicts (both free, never replace Gemini).
ALTER TABLE job_relevance ADD COLUMN local_rank_score    REAL;      -- ranker prediction, 0-100
ALTER TABLE job_relevance ADD COLUMN local_rank_version  TEXT;
ALTER TABLE job_relevance ADD COLUMN local_llm_score     REAL;      -- local pre-score, 0-100
ALTER TABLE job_relevance ADD COLUMN local_llm_flags     TEXT;      -- JSON verdict: workplace, remote, relevant, ...
ALTER TABLE job_relevance ADD COLUMN local_llm_model     TEXT;
ALTER TABLE job_relevance ADD COLUMN local_llm_input_hash TEXT;     -- description hash + prompt version + model
CREATE INDEX idx_job_relevance_rank ON job_relevance (local_rank_score);
