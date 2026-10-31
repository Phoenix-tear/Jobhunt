-- 006_feedback: my dismiss / like / undo actions on jobs (step 3, phase D).
-- Append-only history; the current state of a job is its latest non-undone action.
-- Dismissed jobs are hidden by default; `suggest-rules` reads this table and only PRINTS proposals.

CREATE TABLE job_feedback (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    action      TEXT    NOT NULL CHECK (action IN ('dismiss', 'like', 'undo')),
    reason      TEXT    CHECK (reason IS NULL OR reason IN
                               ('wrong_role', 'wrong_stack', 'too_senior', 'wrong_location', 'company', 'other')),
    created_at  TEXT    NOT NULL
);

CREATE INDEX idx_job_feedback_job     ON job_feedback (job_id, id);
CREATE INDEX idx_job_feedback_action  ON job_feedback (action, reason);

-- One row per job whose latest feedback is a dismiss or a like (an `undo` clears the state).
CREATE VIEW job_feedback_current AS
SELECT f.job_id, f.action, f.reason, f.created_at
FROM job_feedback f
WHERE f.id = (SELECT MAX(id) FROM job_feedback WHERE job_id = f.job_id)
  AND f.action != 'undo';
