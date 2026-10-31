"""Canonical URLs, fingerprints and the idempotent job upsert.

Dedupe rule (also documented in docs/SOURCES.md)
-------------------------------------------
1. Same source: (source, source_job_id) is UNIQUE in `job_sightings`. Seeing the same id again
   only refreshes that sighting and the job; it never creates a second job.
2. Cross source: a *new* sighting is attached to an existing job when
   a. its canonical apply URL equals the job's canonical URL (tracking params stripped, host
      aliases folded, ATS apply-page suffixes removed) AND the URL is job-specific (a bare
      "company.com/careers" never merges two different jobs), or
   b. its fingerprint equals the job's fingerprint AND that job has no sighting from the
      incoming source yet (two postings of one board are different jobs, even when they share
      a title and city).
   Fingerprint = sha256(normalized company | normalized title | location key) where the
   location key is "<country>:<city>" (remote-only postings: "remote:<country>"). So the same
   title in two cities has two different fingerprints and never merges.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobhunt.models import EmploymentType, ExperienceLevel, NormalizedJob

# --- URL canonicalization -----------------------------------------------------------------

_TRACKING_PARAMETERS = frozenset(
    {
        "gclid", "fbclid", "msclkid", "dclid", "mc_cid", "mc_eid", "igshid", "yclid", "twclid",
        "ref", "ref_src", "referrer", "refid", "source", "src", "trk", "trkinfo", "trackingid",
        "tracking", "lipi", "cid", "campaign", "gh_src", "lever-source", "lever-origin", "lever_source",
        "_hsenc", "_hsmi", "hsctatracking", "ccuid", "mkt_tok", "vero_id", "sid", "ss",
    }
)
_HOST_ALIASES = {
    "job-boards.greenhouse.io": "boards.greenhouse.io",
    "job-boards.eu.greenhouse.io": "boards.eu.greenhouse.io",
    "api.lever.co": "jobs.lever.co",
    "api.eu.lever.co": "jobs.eu.lever.co",
}
_APPLY_SUFFIXES = ("/apply", "/application", "/c/new")
_ATS_JOB_URL = re.compile(
    r"^(?:boards(?:\.eu)?\.greenhouse\.io/[^/]+/jobs/\d+"
    r"|jobs(?:\.eu)?\.lever\.co/[^/]+/[0-9a-f-]{8,}"
    r"|jobs\.ashbyhq\.com/[^/]+/[0-9a-f-]{8,}"
    r"|apply\.workable\.com/(?:j/[^/]+|[^/]+/j/[^/]+)"
    r"|(?:jobs|careers)\.smartrecruiters\.com/[^/]+/\d+"
    r"|[^/]+\.recruitee\.com/o/[^/]+)",
    re.IGNORECASE,
)
_SMARTRECRUITERS_SLUG = re.compile(r"(/\d{6,})-[^/]+$")
_JOB_ID_PARAMETERS = frozenset({"gh_jid", "jobid", "job_id", "jid", "id", "jk", "currentjobid", "reqid", "requisitionid", "posting"})
_GENERIC_PATH_SEGMENTS = frozenset({"careers", "career", "jobs", "job", "openings", "positions", "join-us", "work-with-us", "en", "us"})


def canonicalize_url(url: str | None) -> str | None:
    """Strip tracking noise so equal postings compare equal. None for non-http(s) input."""
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None

    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    host = _HOST_ALIASES.get(host, host)

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    for suffix in _APPLY_SUFFIXES:
        if path.lower().endswith(suffix) and len(path) > len(suffix):
            path = path[: -len(suffix)]
            break
    if len(path) > 1:
        path = path.rstrip("/")
    if host.endswith("smartrecruiters.com"):  # /Company/744000123-job-title -> /Company/744000123
        path = _SMARTRECRUITERS_SLUG.sub(r"\1", path)

    kept = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=False)
        if name.lower() not in _TRACKING_PARAMETERS and not name.lower().startswith("utm_")
    ]
    query = urlencode(sorted(kept))
    return urlunsplit(("https", host, path, query, ""))


def is_specific_job_url(canonical_url: str | None) -> bool:
    """True when the URL points at one posting, so equality really means 'same job'."""
    if not canonical_url:
        return False
    parts = urlsplit(canonical_url)
    if _ATS_JOB_URL.match(f"{parts.hostname}{parts.path}"):
        return True
    query_names = {name.lower() for name, _ in parse_qsl(parts.query)}
    if query_names & _JOB_ID_PARAMETERS:
        return True
    segments = [segment for segment in parts.path.split("/") if segment]
    meaningful = [segment for segment in segments if segment.lower() not in _GENERIC_PATH_SEGMENTS]
    # "/jobs/software-engineer-1234" is specific; "/careers" and "/" are not.
    return len(meaningful) >= 1 and len(segments) >= 2


# --- fingerprint --------------------------------------------------------------------------

_LEGAL_SUFFIXES = frozenset(
    {"inc", "incorporated", "llc", "ltd", "limited", "gmbh", "corp", "corporation", "co", "company",
     "plc", "pvt", "private", "ag", "sa", "bv", "lp", "llp", "pte", "oy", "ab", "as", "technologies"}
)
_BRACKETED = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_company_name(name: str | None) -> str:
    """'Acme, Inc.' / 'ACME Corp' -> 'acme'."""
    words = _NON_ALNUM.sub(" ", (name or "").lower().replace("&", " and ")).split()
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    if len(words) > 1 and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def normalize_title(title: str | None) -> str:
    """Lower-case, drop bracketed decorations like '(Remote)', punctuation to spaces."""
    without_brackets = _BRACKETED.sub(" ", (title or "").lower().replace("&", " and "))
    return " ".join(_NON_ALNUM.sub(" ", without_brackets).split())


def location_key(country: str | None, city: str | None, is_remote: bool | None, location_raw: str | None) -> str:
    city_part = _NON_ALNUM.sub(" ", (city or "").lower()).strip()
    country_part = (country or "").upper()
    if city_part:
        return f"{country_part}:{city_part}"
    if is_remote:
        return f"remote:{country_part}"
    if country_part:
        return country_part
    return _NON_ALNUM.sub(" ", (location_raw or "").lower()).strip()


def build_fingerprint(company_name: str | None, title: str | None, key: str) -> str:
    payload = "|".join((normalize_company_name(company_name), normalize_title(title), key))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hash_description(description_text: str | None) -> str | None:
    if not description_text:
        return None
    return hashlib.sha256(description_text.encode("utf-8")).hexdigest()


# --- upsert -------------------------------------------------------------------------------

@dataclass
class UpsertStats:
    created: int = 0     # new `jobs` rows
    merged: int = 0      # new sightings attached to an existing job (cross-source dedupe)
    updated: int = 0     # known sightings whose content changed
    unchanged: int = 0   # known sightings only touched (last_seen_at)
    reopened: int = 0    # closed jobs that reappeared

    def add(self, other: "UpsertStats") -> None:
        for name in ("created", "merged", "updated", "unchanged", "reopened"):
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass
class _Prepared:
    job: NormalizedJob
    canonical_url: str | None
    fingerprint: str
    description_hash: str | None
    company_id: int | None
    has_full_description: bool = field(init=False)

    def __post_init__(self) -> None:
        self.has_full_description = bool(self.job.description_text) and not self.job.needs_description


_SQLITE_VARIABLE_CHUNK = 400


def upsert_jobs(
    connection: sqlite3.Connection,
    jobs: Sequence[NormalizedJob],
    *,
    company_id: int | None,
    seen_at: str,
) -> UpsertStats:
    """Insert/merge/refresh a batch of jobs. The caller owns the surrounding transaction.

    Unchanged known jobs are only "touched" with two executemany statements; the per-row
    path is reserved for new, merged and changed jobs.
    """
    stats = UpsertStats()
    prepared = _prepare_batch(jobs, company_id)
    if not prepared:
        return stats

    existing = _load_existing(connection, prepared)
    touched_sighting_ids: list[tuple[str, int]] = []
    touched_job_ids: list[tuple[str, int]] = []

    for item in prepared:
        row = existing.get((item.job.source, item.job.source_job_id))
        if row is None:
            _insert_or_merge(connection, item, seen_at, stats)
        elif row["status"] == "open" and _is_unchanged(row, item):
            touched_sighting_ids.append((seen_at, row["sighting_id"]))
            touched_job_ids.append((seen_at, row["id"]))
            stats.unchanged += 1
        else:
            _refresh_existing(connection, row, item, seen_at, stats)

    if touched_sighting_ids:
        connection.executemany("UPDATE job_sightings SET last_seen_at = ? WHERE id = ?", touched_sighting_ids)
        connection.executemany(
            "UPDATE jobs SET last_seen_at = ?, missed_runs = 0 WHERE id = ?", touched_job_ids
        )
    return stats


def touch_sightings(
    connection: sqlite3.Connection, source: str, source_job_ids: Iterable[str], seen_at: str
) -> int:
    """Mark sightings as seen without new data (jobs the prefilter now rejects but we hold)."""
    parameters = [(seen_at, source, source_job_id) for source_job_id in source_job_ids]
    if not parameters:
        return 0
    connection.executemany(
        "UPDATE jobs SET last_seen_at = ?, missed_runs = 0 WHERE id IN "
        "(SELECT job_id FROM job_sightings WHERE source = ? AND source_job_id = ?)",
        parameters,
    )
    connection.executemany(
        "UPDATE job_sightings SET last_seen_at = ? WHERE source = ? AND source_job_id = ?",
        parameters,
    )
    return len(parameters)


# -- internals -----------------------------------------------------------------------------

def _prepare_batch(jobs: Sequence[NormalizedJob], company_id: int | None) -> list[_Prepared]:
    prepared: list[_Prepared] = []
    seen_keys: set[tuple[str, str]] = set()
    for job in jobs:
        key = (job.source, job.source_job_id)
        if key in seen_keys:  # adapters dedupe already; this keeps the UNIQUE index safe
            continue
        seen_keys.add(key)
        resolved_company_id = job.company_id if job.company_id is not None else company_id
        canonical = canonicalize_url(job.apply_url)
        fingerprint = build_fingerprint(
            job.company_name,
            job.title,
            location_key(job.country, job.city, job.is_remote, job.location_raw),
        )
        prepared.append(_Prepared(job, canonical, fingerprint, hash_description(job.description_text), resolved_company_id))
    return prepared


def _load_existing(connection: sqlite3.Connection, prepared: Sequence[_Prepared]) -> dict[tuple[str, str], sqlite3.Row]:
    by_source: dict[str, list[str]] = {}
    for item in prepared:
        by_source.setdefault(item.job.source, []).append(item.job.source_job_id)

    found: dict[tuple[str, str], sqlite3.Row] = {}
    for source, source_job_ids in by_source.items():
        for start in range(0, len(source_job_ids), _SQLITE_VARIABLE_CHUNK):
            chunk = source_job_ids[start : start + _SQLITE_VARIABLE_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""SELECT s.id AS sighting_id, s.source, s.source_job_id,
                           (SELECT MIN(id) FROM job_sightings WHERE job_id = j.id) AS primary_sighting_id,
                           j.*
                    FROM job_sightings s JOIN jobs j ON j.id = s.job_id
                    WHERE s.source = ? AND s.source_job_id IN ({placeholders})""",
                [source, *chunk],
            )
            for row in rows:
                found[(row["source"], row["source_job_id"])] = row
    return found


def _is_unchanged(row: sqlite3.Row, item: _Prepared) -> bool:
    job = item.job
    if row["title"] != job.title or row["location_raw"] != job.location_raw:
        return False
    if item.canonical_url and row["canonical_apply_url"] != item.canonical_url:
        return False
    if item.has_full_description and (row["description_hash"] != item.description_hash or row["needs_description"]):
        return False
    comparisons = (
        (row["country"], job.country),
        (row["city"], job.city),
        (row["posted_at"], job.posted_at),
        (row["salary_min"], job.salary_min),
        (row["salary_max"], job.salary_max),
    )
    if any(incoming is not None and stored != incoming for stored, incoming in comparisons):
        return False
    if job.is_remote is not None and row["is_remote"] != int(job.is_remote):
        return False
    if job.employment_type is not EmploymentType.UNKNOWN and row["employment_type"] != job.employment_type:
        return False
    if job.experience_level is not ExperienceLevel.UNKNOWN and row["experience_level"] != job.experience_level:
        return False
    return True


def _raw_json(job: NormalizedJob) -> str | None:
    if job.raw is None:
        return None
    return json.dumps(job.raw, ensure_ascii=False, separators=(",", ":"), default=str)


def _insert_or_merge(connection: sqlite3.Connection, item: _Prepared, seen_at: str, stats: UpsertStats) -> None:
    job = item.job
    target_id = _find_merge_target(connection, item)
    if target_id is not None:
        sighting_id = _insert_sighting(connection, target_id, item, seen_at)
        row = connection.execute(
            """SELECT j.*, ? AS sighting_id,
                      (SELECT MIN(id) FROM job_sightings WHERE job_id = j.id) AS primary_sighting_id
               FROM jobs j WHERE j.id = ?""",
            (sighting_id, target_id),
        ).fetchone()
        if row["status"] == "closed":
            stats.reopened += 1
        _apply_refresh(connection, row, item, seen_at, stats)
        stats.merged += 1
        return

    cursor = connection.execute(
        """INSERT INTO jobs (
               fingerprint, canonical_apply_url, title, company_id, company_name, location_raw,
               country, city, is_remote, employment_type, experience_level, graduation_year_hint,
               description_text, description_hash, needs_description, posted_at, first_seen_at,
               last_seen_at, missed_runs, status, salary_min, salary_max, salary_currency,
               sponsorship_note, raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'open',?,?,?,?,?)""",
        (
            item.fingerprint, item.canonical_url, job.title, item.company_id, job.company_name,
            job.location_raw, job.country, job.city,
            None if job.is_remote is None else int(job.is_remote),
            job.employment_type.value, job.experience_level.value, job.graduation_year_hint,
            job.description_text, item.description_hash,
            int(job.needs_description or not job.description_text),
            job.posted_at, seen_at, seen_at,
            job.salary_min, job.salary_max, job.salary_currency, job.sponsorship_note, _raw_json(job),
        ),
    )
    _insert_sighting(connection, int(cursor.lastrowid), item, seen_at)
    stats.created += 1


def _find_merge_target(connection: sqlite3.Connection, item: _Prepared) -> int | None:
    if item.canonical_url and is_specific_job_url(item.canonical_url):
        row = connection.execute(
            "SELECT id FROM jobs WHERE canonical_apply_url = ? ORDER BY id LIMIT 1", (item.canonical_url,)
        ).fetchone()
        if row:
            return int(row["id"])
    row = connection.execute(
        """SELECT j.id FROM jobs j
           WHERE j.fingerprint = ?
             AND NOT EXISTS (SELECT 1 FROM job_sightings s WHERE s.job_id = j.id AND s.source = ?)
           ORDER BY j.id LIMIT 1""",
        (item.fingerprint, item.job.source),
    ).fetchone()
    return int(row["id"]) if row else None


def _insert_sighting(connection: sqlite3.Connection, job_id: int, item: _Prepared, seen_at: str) -> int:
    job = item.job
    cursor = connection.execute(
        """INSERT INTO job_sightings
               (job_id, source, source_job_id, source_url, attribution_text, first_seen_at, last_seen_at)
           VALUES (?,?,?,?,?,?,?)""",
        (job_id, job.source, job.source_job_id, job.source_url or job.apply_url, job.attribution_text, seen_at, seen_at),
    )
    return int(cursor.lastrowid)


def _refresh_existing(
    connection: sqlite3.Connection, row: sqlite3.Row, item: _Prepared, seen_at: str, stats: UpsertStats
) -> None:
    job = item.job
    connection.execute(
        """UPDATE job_sightings
           SET last_seen_at = ?, source_url = COALESCE(?, source_url),
               attribution_text = COALESCE(?, attribution_text)
           WHERE id = ?""",
        (seen_at, job.source_url or job.apply_url, job.attribution_text, row["sighting_id"]),
    )
    _apply_refresh(connection, row, item, seen_at, stats)
    if row["status"] == "closed":
        stats.reopened += 1
    stats.updated += 1


def _apply_refresh(
    connection: sqlite3.Connection, row: sqlite3.Row, item: _Prepared, seen_at: str, stats: UpsertStats
) -> None:
    """Update the job row from one sighting.

    The earliest sighting is the job's "primary" one and may overwrite identity fields. Later
    (cross-source) sightings only fill gaps, so an aggregator never clobbers ATS data.
    A complete description always beats a truncated one, never the other way round.
    """
    job = item.job
    primary = row["sighting_id"] == row["primary_sighting_id"]

    def pick(stored: object, incoming: object, *, unknown: object = None) -> object:
        has_incoming = incoming is not None and incoming != unknown
        if primary:
            return incoming if has_incoming else stored
        return stored if stored is not None and stored != unknown else (incoming if has_incoming else stored)

    description_text = row["description_text"]
    description_hash = row["description_hash"]
    needs_description = row["needs_description"]
    embedded_reset = False
    if item.has_full_description:
        embedded_reset = description_hash != item.description_hash
        description_text, description_hash, needs_description = job.description_text, item.description_hash, 0
    elif not description_text and job.description_text:
        description_text, description_hash, needs_description = job.description_text, item.description_hash, 1

    is_remote = pick(row["is_remote"], None if job.is_remote is None else int(job.is_remote))
    connection.execute(
        """UPDATE jobs SET
               fingerprint = ?, canonical_apply_url = ?, title = ?, company_id = ?, company_name = ?,
               location_raw = ?, country = ?, city = ?, is_remote = ?, employment_type = ?,
               experience_level = ?, graduation_year_hint = ?, description_text = ?,
               description_hash = ?, needs_description = ?, posted_at = ?, last_seen_at = ?,
               missed_runs = 0, status = 'open', closed_at = NULL, closed_reason = NULL,
               salary_min = ?, salary_max = ?, salary_currency = ?, sponsorship_note = ?,
               raw_json = COALESCE(?, raw_json),
               embedded_at = CASE WHEN ? THEN NULL ELSE embedded_at END
           WHERE id = ?""",
        (
            item.fingerprint if primary else row["fingerprint"],
            pick(row["canonical_apply_url"], item.canonical_url),
            job.title if primary else row["title"],
            row["company_id"] if row["company_id"] is not None else item.company_id,
            pick(row["company_name"], job.company_name or None),
            pick(row["location_raw"], job.location_raw),
            pick(row["country"], job.country),
            pick(row["city"], job.city),
            is_remote,
            pick(row["employment_type"], job.employment_type.value, unknown="unknown"),
            pick(row["experience_level"], job.experience_level.value, unknown="unknown"),
            pick(row["graduation_year_hint"], job.graduation_year_hint),
            description_text, description_hash, needs_description,
            pick(row["posted_at"], job.posted_at),
            seen_at,
            pick(row["salary_min"], job.salary_min),
            pick(row["salary_max"], job.salary_max),
            pick(row["salary_currency"], job.salary_currency),
            pick(row["sponsorship_note"], job.sponsorship_note),
            _raw_json(job) if primary else None,
            1 if embedded_reset else 0,
            row["id"],
        ),
    )
