-- Migration 026: Add explicit provider column to job_embeddings and profile_embeddings (Brief v13)
ALTER TABLE job_embeddings ADD COLUMN provider TEXT;
ALTER TABLE profile_embeddings ADD COLUMN provider TEXT;

UPDATE job_embeddings SET provider = CASE WHEN model LIKE 'ollama:%' THEN 'ollama' ELSE 'gemini' END WHERE provider IS NULL;
UPDATE profile_embeddings SET provider = CASE WHEN model LIKE 'ollama:%' THEN 'ollama' ELSE 'gemini' END WHERE provider IS NULL;
