-- 003_remote_eligibility: who a remote job is actually open to (step 3, phase A).
-- Additive only: new nullable/defaulted columns on `jobs`; no stored value is rewritten.
-- Populated by `python -m jobhunt backfill-remote` and by every `match` run (new rows only).

ALTER TABLE jobs ADD COLUMN workplace_type TEXT NOT NULL DEFAULT 'unknown'
    CHECK (workplace_type IN ('remote', 'hybrid', 'onsite', 'unknown'));

ALTER TABLE jobs ADD COLUMN remote_scope TEXT NOT NULL DEFAULT 'unknown'
    CHECK (remote_scope IN ('worldwide', 'regional', 'country_restricted', 'unknown'));

-- JSON list of ISO-2 country codes and region tags (EMEA, EUROPE, APAC, AMER, NA, LATAM).
ALTER TABLE jobs ADD COLUMN remote_regions TEXT;

-- 1 = open to someone living in the home country, 0 = restricted elsewhere, NULL = unclear
-- or not applicable (hybrid / on-site jobs).
ALTER TABLE jobs ADD COLUMN remote_eligible_india INTEGER
    CHECK (remote_eligible_india IS NULL OR remote_eligible_india IN (0, 1));

-- Posting mentions needing existing work authorization / refusing sponsorship.
ALTER TABLE jobs ADD COLUMN work_auth_required INTEGER NOT NULL DEFAULT 0
    CHECK (work_auth_required IN (0, 1));

-- How the verdict was reached ("structured" | "text" | "description" | "llm" | "none") and a
-- short human-readable evidence string for the UI / eval tooling.
ALTER TABLE jobs ADD COLUMN remote_source TEXT;
ALTER TABLE jobs ADD COLUMN remote_evidence TEXT;

-- Inputs the derivation was computed from (parser version | description hash | location);
-- a row is recomputed only when this differs, so backfills are incremental.
ALTER TABLE jobs ADD COLUMN remote_basis TEXT;

CREATE INDEX idx_jobs_workplace ON jobs (status, workplace_type, remote_eligible_india);
