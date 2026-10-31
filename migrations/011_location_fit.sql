-- Location policy: where the job is and whether it is somewhere I want (reversible, nothing deleted).
ALTER TABLE job_relevance ADD COLUMN location_fit     TEXT;     -- india | remote_open_to_india | remote_unclear |
                                                               -- abroad_unknown_workplace | wrong_location | unknown
ALTER TABLE job_relevance ADD COLUMN location_reason  TEXT;
ALTER TABLE job_relevance ADD COLUMN location_matched TEXT;     -- which location text qualified the job
CREATE INDEX idx_job_relevance_location ON job_relevance (location_fit);
