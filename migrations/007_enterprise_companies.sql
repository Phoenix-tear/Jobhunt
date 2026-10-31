-- Large employers that do not use Greenhouse/Lever/Ashby: which career platform each one runs and
-- how an adapter should address it. Additive: no existing table changes.
CREATE TABLE enterprise_companies (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    careers_url     TEXT    NOT NULL,
    detected_ats    TEXT    NOT NULL DEFAULT 'unknown',   -- workday | oracle | eightfold | ... | unknown
    adapter_params  TEXT,                                 -- JSON: tenant / host / site (Workday), host / site code (Oracle)
    status          TEXT    NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'detected', 'needs_adapter', 'unreachable', 'blocked', 'skipped')),
    evidence        TEXT,                                 -- what proved the platform (host seen, marker, final URL)
    last_checked_at TEXT,
    last_error      TEXT,
    last_fetched_at TEXT,
    jobs_found      INTEGER NOT NULL DEFAULT 0,           -- jobs returned by the last adapter run (after caps)
    created_at      TEXT    NOT NULL
);

CREATE INDEX idx_enterprise_ats ON enterprise_companies (detected_ats, status);
