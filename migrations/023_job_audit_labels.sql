-- Migration 023: audit labels table for disagreement audit import (Brief v10b Section 5)
CREATE TABLE IF NOT EXISTS job_audit_labels (
    job_id INTEGER NOT NULL,
    head TEXT NOT NULL,
    label REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'audit',
    my_verdict TEXT NOT NULL,
    my_note TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, head)
);
