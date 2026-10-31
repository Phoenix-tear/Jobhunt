-- Rule-based seniority (junior | unspecified | mid | senior | executive) and the evidence for it.
ALTER TABLE job_relevance ADD COLUMN seniority_level    TEXT;
ALTER TABLE job_relevance ADD COLUMN seniority_evidence TEXT;
CREATE INDEX idx_job_relevance_seniority ON job_relevance (seniority_level);
