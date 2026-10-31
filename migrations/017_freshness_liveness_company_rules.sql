-- v6: stale / closed jobs. Additive only. Nothing here deletes or rewrites existing rows.
--   job_auto_hidden : jobs moved to the "Auto-hidden" view (reason auto_stale | closed_checked); Restore sets restored_at,
--                     and a restored job is never hidden automatically again.
--   job_freshness   : the age band (fresh | aging | stale | unknown) computed by jobhunt.freshness, for the chip and filter.
--   job_liveness    : the last "is it still open?" check per job (also the 24 hour cache of the daily shortlist check).
--   jobs.closed_reason : why a job was closed locally (closed_checked | reported_closed); NULL = the board lifecycle.
ALTER TABLE jobs ADD COLUMN closed_reason TEXT;

CREATE TABLE job_auto_hidden (
    job_id      INTEGER PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    reason      TEXT NOT NULL CHECK (reason IN ('auto_stale', 'closed_checked')),
    detail      TEXT,
    hidden_at   TEXT NOT NULL,
    restored_at TEXT
);
CREATE INDEX idx_job_auto_hidden_active ON job_auto_hidden (reason) WHERE restored_at IS NULL;

CREATE TABLE job_freshness (
    job_id      INTEGER PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    band        TEXT NOT NULL CHECK (band IN ('fresh', 'aging', 'stale', 'unknown')),
    age_days    INTEGER,
    basis       TEXT,                    -- posted | first_seen | none
    computed_at TEXT NOT NULL
);
CREATE INDEX idx_job_freshness_band ON job_freshness (band);

CREATE TABLE job_liveness (
    job_id      INTEGER PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    result      TEXT NOT NULL CHECK (result IN ('open', 'closed', 'uncertain')),
    detail      TEXT,
    http_status INTEGER,
    method      TEXT,                    -- ats_api | page
    trigger     TEXT,                    -- button | match
    checked_at  TEXT NOT NULL
);
