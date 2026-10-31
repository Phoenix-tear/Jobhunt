-- 002_search_and_listing_indexes: full-text search and listing indexes for the website.
-- Additive only: no existing table or column is changed.

-- ---------------------------------------------------------------------------
-- Full-text search over title, company, location and description.
--
-- External-content FTS5 table: the index stores no copy of the text, FTS5 reads it back from
-- `jobs` by rowid (content_rowid = jobs.id). Triggers keep it in sync (the pattern from the
-- SQLite FTS5 docs: a 'delete' command carrying the OLD values, then an insert of the NEW ones).
--
-- Tokenizer: porter stemming over unicode61 with diacritics removed, so "engineering",
-- "engineers" and "Engineer" all match; prefix indexes (2, 3) make type-ahead cheap.
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE jobs_fts USING fts5(
    title,
    company_name,
    location_raw,
    description_text,
    content = 'jobs',
    content_rowid = 'id',
    tokenize = 'porter unicode61 remove_diacritics 2',
    prefix = '2 3'
);

CREATE TRIGGER jobs_fts_after_insert AFTER INSERT ON jobs BEGIN
    INSERT INTO jobs_fts (rowid, title, company_name, location_raw, description_text)
    VALUES (new.id, new.title, new.company_name, new.location_raw, new.description_text);
END;

CREATE TRIGGER jobs_fts_after_delete AFTER DELETE ON jobs BEGIN
    INSERT INTO jobs_fts (jobs_fts, rowid, title, company_name, location_raw, description_text)
    VALUES ('delete', old.id, old.title, old.company_name, old.location_raw, old.description_text);
END;

-- Ingestion updates `jobs` constantly (last_seen_at, missed_runs...). The WHEN guard makes the
-- trigger a no-op unless an indexed column actually changed, so those updates cost nothing.
CREATE TRIGGER jobs_fts_after_update
AFTER UPDATE OF title, company_name, location_raw, description_text ON jobs
WHEN old.title IS NOT new.title
  OR old.company_name IS NOT new.company_name
  OR old.location_raw IS NOT new.location_raw
  OR old.description_text IS NOT new.description_text
BEGIN
    INSERT INTO jobs_fts (jobs_fts, rowid, title, company_name, location_raw, description_text)
    VALUES ('delete', old.id, old.title, old.company_name, old.location_raw, old.description_text);
    INSERT INTO jobs_fts (rowid, title, company_name, location_raw, description_text)
    VALUES (new.id, new.title, new.company_name, new.location_raw, new.description_text);
END;

-- Index the rows that already exist.
INSERT INTO jobs_fts (jobs_fts) VALUES ('rebuild');

-- ---------------------------------------------------------------------------
-- Listing indexes.
--
-- `jobs` rows are wide (descriptions run to tens of KB) and every column stored after
-- `description_text` (status, posted_at, needs_description...) sits behind overflow pages, so a
-- filtering scan of the table is slow. These indexes are COVERING for the website's list and
-- count queries: they contain every filter column plus the sort key, so those queries are
-- answered from the index alone and never touch the wide rows.
--
-- "Newest" = when the posting went live, else when we first saw it (an expression key).
-- ---------------------------------------------------------------------------
CREATE INDEX idx_jobs_list_newest ON jobs (
    status, COALESCE(posted_at, first_seen_at) DESC,
    experience_level, employment_type, country, is_remote, needs_description
);
CREATE INDEX idx_jobs_list_company ON jobs (
    status, LOWER(company_name),
    experience_level, employment_type, country, is_remote, needs_description, COALESCE(posted_at, first_seen_at)
);
CREATE INDEX idx_jobs_list_title ON jobs (
    status, LOWER(title),
    experience_level, employment_type, country, is_remote, needs_description, COALESCE(posted_at, first_seen_at)
);

-- Kanban board and "is this job saved?" lookups.
CREATE INDEX idx_applications_stage ON applications (stage, updated_at);

