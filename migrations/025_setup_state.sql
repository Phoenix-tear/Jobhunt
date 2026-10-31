-- Migration 025: setup state record for first-run wizard and settings (Brief v12)
-- Additive only. An existing install (jobs > 0 and profile created) is marked completed automatically on first start.

CREATE TABLE IF NOT EXISTS setup_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    completed       BOOLEAN NOT NULL DEFAULT 0,
    step            TEXT    NOT NULL DEFAULT 'welcome',
    completed_at    TEXT,
    schema_version  INTEGER NOT NULL DEFAULT 1
);
