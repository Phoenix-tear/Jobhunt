-- v5: what a decision replaced, so "Undo" can put the tracker entry, the resume and the dismiss state back.
ALTER TABLE job_decisions ADD COLUMN prior_json TEXT;
