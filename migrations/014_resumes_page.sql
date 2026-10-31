-- Resumes page: global resume numbers (R-0001...), per-job versions, lifecycle and the "what changed" record.
-- Additive only. `status` keeps its meaning (ok | fallback | failed = how the run ended); the new `lifecycle` column is
-- draft | approved | sent | superseded. No file is moved or deleted.
ALTER TABLE tailored_resumes ADD COLUMN resume_number        INTEGER;
ALTER TABLE tailored_resumes ADD COLUMN version              INTEGER;
ALTER TABLE tailored_resumes ADD COLUMN parent_resume_id     INTEGER REFERENCES tailored_resumes (id);
ALTER TABLE tailored_resumes ADD COLUMN base                 TEXT;
ALTER TABLE tailored_resumes ADD COLUMN recipe               TEXT;
ALTER TABLE tailored_resumes ADD COLUMN summary_variant      TEXT;
ALTER TABLE tailored_resumes ADD COLUMN fill_ratio           REAL;
ALTER TABLE tailored_resumes ADD COLUMN page_count           INTEGER;
ALTER TABLE tailored_resumes ADD COLUMN validator_status     TEXT;
ALTER TABLE tailored_resumes ADD COLUMN keywords_matched_json TEXT;
ALTER TABLE tailored_resumes ADD COLUMN keywords_missing_json TEXT;
ALTER TABLE tailored_resumes ADD COLUMN advice_source        TEXT;      -- gemini | deterministic
ALTER TABLE tailored_resumes ADD COLUMN profile_hash         TEXT;      -- NULL: not recorded (older rows)
ALTER TABLE tailored_resumes ADD COLUMN change_summary_json  TEXT;      -- NULL until computed; see tailoring/changes.py
ALTER TABLE tailored_resumes ADD COLUMN lifecycle            TEXT NOT NULL DEFAULT 'draft';
ALTER TABLE tailored_resumes ADD COLUMN sent_at              TEXT;
ALTER TABLE tailored_resumes ADD COLUMN archived             INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tailored_resumes ADD COLUMN file_note            TEXT;      -- e.g. the PDF was overwritten by a later run

-- numbering in chronological (id) order
UPDATE tailored_resumes SET resume_number = (SELECT COUNT(*) FROM tailored_resumes b WHERE b.id <= tailored_resumes.id);
UPDATE tailored_resumes SET version = (SELECT COUNT(*) FROM tailored_resumes b
                                       WHERE b.job_id = tailored_resumes.job_id AND b.id <= tailored_resumes.id);
UPDATE tailored_resumes SET parent_resume_id = (SELECT MAX(b.id) FROM tailored_resumes b
                                                WHERE b.job_id = tailored_resumes.job_id AND b.id < tailored_resumes.id);

-- fields that can be read from what was stored
UPDATE tailored_resumes SET
    base = base_profile,
    recipe = json_extract(plan_json, '$.recipe'),
    summary_variant = json_extract(plan_json, '$.summary_variant'),
    fill_ratio = json_extract(plan_json, '$.fill'),
    page_count = pages,
    validator_status = CASE WHEN validation_json IS NULL THEN NULL
                            WHEN json_extract(validation_json, '$.ok') IN (1, 'true') THEN 'ok' ELSE 'failed' END,
    keywords_matched_json = CASE WHEN json_type(plan_json, '$.matched') = 'array'
        THEN (SELECT json_group_array(json_extract(value, '$.keyword')) FROM json_each(plan_json, '$.matched')) END,
    keywords_missing_json = CASE WHEN json_type(plan_json, '$.missing') = 'array'
        THEN json_extract(plan_json, '$.missing') END,
    advice_source = CASE WHEN status = 'ok' AND COALESCE(model, '') != '' AND mode != 'deterministic'
                         THEN 'gemini' ELSE 'deterministic' END
WHERE plan_json IS NOT NULL AND json_valid(plan_json);

-- lifecycle: approved stays approved; the newest run of a job is the draft; older ones are superseded
UPDATE tailored_resumes SET lifecycle = CASE
    WHEN approved = 1 THEN 'approved'
    WHEN id = (SELECT MAX(b.id) FROM tailored_resumes b WHERE b.job_id = tailored_resumes.job_id) THEN 'draft'
    ELSE 'superseded' END;
UPDATE tailored_resumes SET lifecycle = 'sent',
       sent_at = (SELECT COALESCE(a.applied_at, a.updated_at) FROM applications a
                  WHERE a.tailored_resume_id = tailored_resumes.id LIMIT 1)
WHERE id IN (SELECT tailored_resume_id FROM applications WHERE tailored_resume_id IS NOT NULL);

-- every run of a job wrote the same resume.pdf: only the newest row's file still holds its own content
UPDATE tailored_resumes SET file_note = 'file overwritten by a later run of the same job'
WHERE pdf_path IS NOT NULL
  AND id != (SELECT MAX(b.id) FROM tailored_resumes b WHERE b.job_id = tailored_resumes.job_id)
  AND pdf_path = (SELECT b.pdf_path FROM tailored_resumes b
                  WHERE b.job_id = tailored_resumes.job_id ORDER BY b.id DESC LIMIT 1);

CREATE UNIQUE INDEX idx_tailored_resume_number ON tailored_resumes (resume_number);
CREATE INDEX idx_tailored_lifecycle ON tailored_resumes (lifecycle, archived, id);
