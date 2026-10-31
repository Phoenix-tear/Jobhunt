-- v7: cover letters and answers are written in stages (plan -> write -> edit) and my own edits are kept as versions.
-- Additive only. Nothing here changes or deletes an existing row; old rows read as pipeline 'single' with no stage data.
--   stages_json    : one entry per stage {stage, model, input_tokens, output_tokens, cost_usd, accepted, note}
--   plan_json      : the validated paragraph plan (intent + fact ids per block) the letter was written from
--   quality_json   : the deterministic language-quality report {ok, warnings, stats}
--   pipeline       : single (v1 one-call) | staged (plan/write/edit) | user_edit (my edit, no model call)
--   parent_id      : for a user edit, the generated version it was based on
--   used_for_export: 1 on the version that .txt / .pdf export and "copy" use for this job (at most one per job and kind)
ALTER TABLE generated_texts ADD COLUMN stages_json     TEXT;
ALTER TABLE generated_texts ADD COLUMN plan_json       TEXT;
ALTER TABLE generated_texts ADD COLUMN quality_json    TEXT;
ALTER TABLE generated_texts ADD COLUMN pipeline        TEXT NOT NULL DEFAULT 'single';
ALTER TABLE generated_texts ADD COLUMN parent_id       INTEGER REFERENCES generated_texts (id) ON DELETE SET NULL;
ALTER TABLE generated_texts ADD COLUMN used_for_export INTEGER NOT NULL DEFAULT 0;
