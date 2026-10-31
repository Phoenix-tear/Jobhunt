-- v5: eligibility flags and learned rules on the relevance row, my Saved / Applied / Not applying decisions, and the
-- rule proposals I approved or rejected. Additive only (job_feedback keeps its CHECK constraints; Not applying is also
-- mirrored there as a legacy dismiss so every existing "hide dismissed" query keeps working).
ALTER TABLE job_relevance ADD COLUMN eligibility_json TEXT;     -- [{kind, strength, snippet}] from matching/eligibility.py
ALTER TABLE job_relevance ADD COLUMN learned_json TEXT;         -- [{id, demote, penalty}] learned rules that applied

CREATE TABLE job_decisions (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    action      TEXT    NOT NULL CHECK (action IN ('saved', 'applied', 'not_applying', 'undo')),
    reason      TEXT,                       -- not_applying: ineligible_phd_or_masters, wrong_role, ... (validated in code)
    note        TEXT,
    how         TEXT,                       -- applied: company_site | linkedin | referral | email | other
    resume_id   INTEGER REFERENCES tailored_resumes (id),
    happened_at TEXT    NOT NULL,           -- when I applied (may differ from created_at)
    created_at  TEXT    NOT NULL
);
CREATE INDEX idx_job_decisions_job ON job_decisions (job_id, id);
CREATE INDEX idx_job_decisions_action ON job_decisions (action, reason);

-- the latest decision per job; an undo clears it
CREATE VIEW job_decision_current AS
SELECT d.job_id, d.action, d.reason, d.note, d.how, d.resume_id, d.happened_at, d.created_at
FROM job_decisions d
WHERE d.id = (SELECT MAX(id) FROM job_decisions WHERE job_id = d.job_id) AND d.action != 'undo';

CREATE TABLE rule_decisions (
    proposal_key  TEXT PRIMARY KEY,         -- e.g. title_phrase:researcher
    status        TEXT NOT NULL CHECK (status IN ('approved', 'rejected')),
    decided_at    TEXT NOT NULL,
    evidence_json TEXT
);
