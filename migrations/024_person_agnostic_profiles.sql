-- Migration 024: person-agnostic profile tables (Brief v11 Phase 3)
-- Additive only: profile_embeddings_v2, job_base_similarity_v2, profiles_v2 keyed by base_id without CHECK constraint on name.
-- Backfilled from profile_embeddings, job_relevance, and profiles. Old tables kept completely untouched.

CREATE TABLE IF NOT EXISTS profile_embeddings_v2 (
    base_id         TEXT    NOT NULL,
    model           TEXT    NOT NULL,
    dimensions      INTEGER NOT NULL,
    prompt_version  TEXT    NOT NULL,
    text_hash       TEXT    NOT NULL,
    vector          BLOB    NOT NULL,
    created_at      TEXT    NOT NULL,
    PRIMARY KEY (base_id, model)
);

CREATE TABLE IF NOT EXISTS job_base_similarity_v2 (
    job_id          INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    base_id         TEXT    NOT NULL,
    similarity      REAL    NOT NULL,
    computed_at     TEXT    NOT NULL,
    PRIMARY KEY (job_id, base_id)
);
CREATE INDEX IF NOT EXISTS idx_job_base_similarity_v2_base ON job_base_similarity_v2 (base_id, similarity);

CREATE TABLE IF NOT EXISTS profiles_v2 (
    base_id        TEXT PRIMARY KEY,
    source_file    TEXT NOT NULL,
    source_hash    TEXT NOT NULL,
    text_hash      TEXT NOT NULL,
    stripped_text  TEXT NOT NULL,
    pii_removed    TEXT,
    built_at       TEXT NOT NULL
);

-- Backfill from existing profile_embeddings
INSERT OR IGNORE INTO profile_embeddings_v2 (base_id, model, dimensions, prompt_version, text_hash, vector, created_at)
SELECT profile_name, model, dimensions, prompt_version, text_hash, vector, created_at
FROM profile_embeddings;

-- Backfill from existing profiles
INSERT OR IGNORE INTO profiles_v2 (base_id, source_file, source_hash, text_hash, stripped_text, pii_removed, built_at)
SELECT name, source_file, source_hash, text_hash, stripped_text, pii_removed, built_at
FROM profiles;

-- Backfill similarity values from job_relevance
INSERT OR IGNORE INTO job_base_similarity_v2 (job_id, base_id, similarity, computed_at)
SELECT job_id, 'ai', similarity_ai, computed_at
FROM job_relevance
WHERE similarity_ai IS NOT NULL;

INSERT OR IGNORE INTO job_base_similarity_v2 (job_id, base_id, similarity, computed_at)
SELECT job_id, 'fullstack', similarity_fullstack, computed_at
FROM job_relevance
WHERE similarity_fullstack IS NOT NULL;
