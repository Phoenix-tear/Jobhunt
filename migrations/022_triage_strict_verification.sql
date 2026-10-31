-- Migration 022: Strict evidence verification and audit-ready triage
-- Additive only: preserves old verified_json for audit and adds Wilson intervals to acting rules.

ALTER TABLE job_llm_verdicts ADD COLUMN verified_json_v1 TEXT;
ALTER TABLE job_triage_acting_rules ADD COLUMN ci_lower REAL;
ALTER TABLE job_triage_acting_rules ADD COLUMN ci_upper REAL;
ALTER TABLE job_triage_acting_rules ADD COLUMN rule_desc TEXT;
