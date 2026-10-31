"""All SQL for the website, built from fixed fragments; user values only ever travel as parameters.

The employment-type convention lives here and only here: the stored `employment_type` is never
changed, the API *derives* an effective type, `unknown` -> `full_time` unless the experience
level is `intern` (then `internship`).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from jobhunt.api import signals
from jobhunt import decisions
from jobhunt.matching.company_rules import level_word_in_title
from jobhunt.matching.eligibility import INFO_ONLY_KINDS, FLAG_LABELS, likely_ineligible_note
from jobhunt.api.schemas import STAGES
from jobhunt.api.search import to_match_expression
from jobhunt.timeutil import to_iso, utc_now

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
MAX_PAGE = 100_000
MAX_URL_CHARS = 2048

# Effective employment type, shared by the SELECT list, the filter and the facets.
EFFECTIVE_TYPE_SQL = (
    "CASE WHEN j.employment_type <> 'unknown' THEN j.employment_type "
    "WHEN j.experience_level = 'intern' THEN 'internship' ELSE 'full_time' END"
)
# What "newest" means: when the posting went live, else when we first saw it.
LISTED_AT_SQL = "COALESCE(j.posted_at, j.first_seen_at)"

# Sorting never changes WHICH jobs match (that is the filters' job), only their order. Every key ends in the unique job id,
# so pages are stable. Keys are whitelisted here; the API validates `sort` against this dict.
_NEWEST_POSTED = "(j.posted_at IS NULL) ASC, j.posted_at DESC, j.first_seen_at DESC, j.id DESC"
_ORDER_BY = {
    "newest": f"{LISTED_AT_SQL} DESC, j.id DESC",   # legacy API key: posted_at, else first_seen_at
    "newest_posted": _NEWEST_POSTED,          # posted_at newest first, jobs without a date last, ties by first_seen_at
    "newest_seen": "j.first_seen_at DESC, j.id DESC",
    "oldest": "(j.posted_at IS NULL) ASC, j.posted_at ASC, j.first_seen_at ASC, j.id ASC",
    "match": f"(r.final_score IS NULL) ASC, r.final_score DESC, {_NEWEST_POSTED}",
    "company": "LOWER(j.company_name) ASC, j.id DESC",
    "title": "LOWER(j.title) ASC, j.id DESC",
}
SORT_KEYS = tuple(_ORDER_BY) + ("relevance",)
CATEGORIES = ("relevant", "target", "maybe", "all", "filtered_out")

_ATS_PRIORITY = {"greenhouse": 0, "lever": 0, "ashby": 0, "workable": 0, "smartrecruiters": 0, "recruitee": 0, "workday": 0, "oracle": 0, "amazon_jobs": 0, "manual": 1}


_RELEVANCE_JOIN = "LEFT JOIN job_relevance r ON r.job_id = j.id"


def safe_http_url(value: str | None) -> str | None:
    """Return `value` only when it is a plain http(s) URL with a host (anything else is not linkable)."""
    if not value or len(value) > MAX_URL_CHARS or any(ord(char) < 33 for char in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    return value if parts.scheme in {"http", "https"} and parts.hostname else None


@dataclass
class JobFilters:
    q: str | None = None
    levels: list[str] = field(default_factory=list)
    employment_types: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    remote: bool | None = None
    sources: list[str] = field(default_factory=list)
    status: str = "open"
    hide_senior: bool = True
    new_within_hours: int | None = None
    needs_description: bool | None = None
    # relevance layer
    relevant_only: bool = True               # hide jobs the gate excluded
    gate_statuses: list[str] = field(default_factory=list)
    role_families: list[str] = field(default_factory=list)
    best_profile: str | None = None
    min_score: float | None = None
    workplace_types: list[str] = field(default_factory=list)
    remote_scope: str | None = None
    remote_eligible_india: bool | None = None
    include_dismissed: bool = False
    junior_only: bool = False                # explicit junior roles only
    location_fits: list[str] = field(default_factory=list)
    include_other_locations: bool = False    # also show wrong_location / abroad_unknown_workplace
    view: str = "open"                       # open | saved | applied | not_applying | auto_hidden | all
    category: str | None = None              # relevant | target | maybe | all | filtered_out (None: the legacy flags above)
    freshness: list[str] = field(default_factory=list)   # fresh | aging | unknown (stale jobs live in the auto_hidden view)


DECIDED_VIEWS = frozenset({"saved", "applied", "not_applying"})
ACTIVE_HIDDEN_SQL = "EXISTS (SELECT 1 FROM job_auto_hidden h WHERE h.job_id = j.id AND h.restored_at IS NULL)"
LOCATION_POLICY_SQL = "COALESCE(r.location_fit, 'unknown') IN ('india', 'remote_open_to_india', 'remote_unclear', 'unknown')"


def _placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def _category_flags(filters: JobFilters) -> tuple[list[str], bool, bool]:
    """(gate statuses to require, hide-excluded, apply the location policy) for the category control.

    The category is its own control: Relevant = target + maybe inside the location policy; Target / Maybe = that gate status
    inside the same location policy; All = every job (including the ones still waiting for a verdict); Filtered out = what
    the gate excluded. A job the gate has not judged yet (just ingested) is in All only: showing it under Relevant is what
    made "Newest first" look like "all jobs" while an ingest was running (they have no score, so Best match hides them).
    `category=None` keeps the older flag-based behaviour (gate_statuses / relevant_only)."""
    category = filters.category
    if category is None:
        return filters.gate_statuses, filters.relevant_only, filters.relevant_only and not filters.gate_statuses
    if category == "relevant":
        return ["target", "maybe"], False, True
    if category in ("target", "maybe"):
        return [category], False, True
    if category == "filtered_out":
        return ["excluded"], False, False
    return [], False, False


def build_where(filters: JobFilters, now: datetime) -> tuple[str, list[object]]:
    """(WHERE fragment, parameters) for everything except free text. Only fixed SQL text is concatenated."""
    clauses: list[str] = []
    params: list[object] = []

    keeps_closed = filters.view in DECIDED_VIEWS or filters.view == "auto_hidden"
    if filters.status in {"open", "closed"} and not keeps_closed:
        clauses.append("j.status = ?")
        params.append(filters.status)

    if filters.levels:
        clauses.append(f"j.experience_level IN ({_placeholders(filters.levels)})")
        params.extend(filters.levels)
    if filters.hide_senior and "senior" not in filters.levels:
        clauses.append("j.experience_level <> 'senior'")

    if filters.employment_types:
        clauses.append(f"({EFFECTIVE_TYPE_SQL}) IN ({_placeholders(filters.employment_types)})")
        params.extend(filters.employment_types)

    if filters.countries:
        known = [code for code in filters.countries if code != "unknown"]
        parts: list[str] = []
        if known:
            parts.append(f"j.country IN ({_placeholders(known)})")
            params.extend(known)
        if len(known) != len(filters.countries):
            parts.append("j.country IS NULL")
        clauses.append("(" + " OR ".join(parts) + ")")

    if filters.remote is True:      # the stored flag, or a remote workplace parsed from the posting
        clauses.append("(j.is_remote = 1 OR j.workplace_type = 'remote')")
    elif filters.remote is False:
        clauses.append("j.is_remote = 0")

    if filters.sources:
        clauses.append(
            "EXISTS (SELECT 1 FROM job_sightings s WHERE s.job_id = j.id "
            f"AND s.source IN ({_placeholders(filters.sources)}))"
        )
        params.extend(filters.sources)

    if filters.new_within_hours is not None:
        clauses.append(f"{LISTED_AT_SQL} >= ?")
        params.append(to_iso(now - timedelta(hours=filters.new_within_hours)))

    if filters.needs_description is not None:
        clauses.append("j.needs_description = ?")
        params.append(1 if filters.needs_description else 0)

    # my decisions: Open hides jobs I applied to or am not applying to; the decided views always show their jobs
    decided_view = filters.view in DECIDED_VIEWS or filters.view == "auto_hidden"
    if filters.view in ("open", "all"):
        clauses.append(f"NOT {ACTIVE_HIDDEN_SQL}")        # stale / closed jobs wait in the Auto-hidden view
    if filters.view == "auto_hidden":
        clauses.append(ACTIVE_HIDDEN_SQL)
    if filters.view == "open":
        clauses.append("NOT EXISTS (SELECT 1 FROM job_decision_current d WHERE d.job_id = j.id "
                       "AND d.action IN ('applied', 'not_applying'))")
        clauses.append("NOT EXISTS (SELECT 1 FROM applications a2 WHERE a2.job_id = j.id AND a2.stage != 'saved')")
    elif filters.view == "saved":
        clauses.append("(EXISTS (SELECT 1 FROM job_decision_current d WHERE d.job_id = j.id AND d.action = 'saved') "
                       "OR EXISTS (SELECT 1 FROM applications a2 WHERE a2.job_id = j.id AND a2.stage = 'saved'))")
    elif filters.view == "applied":
        clauses.append("(EXISTS (SELECT 1 FROM job_decision_current d WHERE d.job_id = j.id AND d.action = 'applied') "
                       "OR EXISTS (SELECT 1 FROM applications a2 WHERE a2.job_id = j.id AND a2.stage != 'saved'))")
    elif filters.view == "not_applying":
        clauses.append("(EXISTS (SELECT 1 FROM job_decision_current d WHERE d.job_id = j.id AND d.action = 'not_applying') "
                       "OR EXISTS (SELECT 1 FROM job_feedback_current f WHERE f.job_id = j.id AND f.action = 'dismiss'))")

    # relevance layer (jobs the gate has not judged yet have no job_relevance row and stay visible)
    gate_statuses, relevant_only, apply_location_policy = _category_flags(filters)
    if gate_statuses:
        clauses.append(f"r.gate_status IN ({_placeholders(gate_statuses)})")
        params.extend(gate_statuses)
    elif relevant_only and not decided_view:
        clauses.append("(r.gate_status IS NULL OR r.gate_status <> 'excluded')")
    if filters.location_fits:
        clauses.append(f"COALESCE(r.location_fit, 'unknown') IN ({_placeholders(filters.location_fits)})")
        params.extend(filters.location_fits)
    elif apply_location_policy and not filters.include_other_locations and not decided_view:
        clauses.append(LOCATION_POLICY_SQL)
    if filters.freshness:
        clauses.append("COALESCE((SELECT fr.band FROM job_freshness fr WHERE fr.job_id = j.id), 'unknown') "
                       f"IN ({_placeholders(filters.freshness)})")
        params.extend(filters.freshness)
    if filters.role_families:
        clauses.append(f"r.role_family IN ({_placeholders(filters.role_families)})")
        params.extend(filters.role_families)
    if filters.best_profile:
        clauses.append("r.best_profile = ?")
        params.append(filters.best_profile)
    if filters.junior_only:
        clauses.append("r.seniority_level = 'junior'")
    if filters.min_score is not None:
        clauses.append("r.final_score >= ?")
        params.append(filters.min_score)
    if filters.workplace_types:
        clauses.append(f"j.workplace_type IN ({_placeholders(filters.workplace_types)})")
        params.extend(filters.workplace_types)
    if filters.remote_scope:
        clauses.append("j.remote_scope = ?")
        params.append(filters.remote_scope)
    if filters.remote_eligible_india is True:
        clauses.append("j.remote_eligible_india = 1")
    elif filters.remote_eligible_india is False:
        clauses.append("j.remote_eligible_india = 0")
    if not filters.include_dismissed and filters.view in ("open", "saved"):
        clauses.append("NOT EXISTS (SELECT 1 FROM job_feedback_current f WHERE f.job_id = j.id AND f.action = 'dismiss')")

    return (" AND ".join(clauses) if clauses else "1 = 1"), params


def list_job_ids(
    connection: sqlite3.Connection,
    filters: JobFilters,
    *,
    sort: str,
    page: int,
    page_size: int,
    now: datetime,
) -> tuple[int, list[int], str]:
    """(total matches, ids of the requested page in order, sort actually applied).

    Free text is applied as `j.id IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)`: the
    planner then walks a covering index in sort order and only probes the (small) FTS result
    set, never reading the wide `jobs` rows. Relevance order is the one case that starts from
    FTS: bm25 over the matches that also pass the filters (again via the covering index).
    """
    where, params = build_where(filters, now)
    match = to_match_expression(filters.q)

    applied_sort = sort
    if applied_sort == "relevance" and match is None:
        applied_sort = "newest"

    page = min(max(page, 1), MAX_PAGE)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    offset = (page - 1) * page_size

    # The unary `+` stops SQLite from planning "probe jobs by primary key for every FTS hit" (which
    # reads the wide rows); it keeps walking the covering index in sort order and tests membership.
    text_clause = " AND +j.id IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)" if match is not None else ""
    text_params: list[object] = [match] if match is not None else []
    total = connection.execute(
        f"SELECT COUNT(*) FROM jobs j {_RELEVANCE_JOIN} WHERE {where}{text_clause}", [*params, *text_params]
    ).fetchone()[0]

    if applied_sort == "relevance":
        # Score every match once (materialized), keep those that pass the filters, order by score.
        rows = connection.execute(
            f"""WITH hits AS MATERIALIZED (
                    SELECT rowid AS id, bm25(jobs_fts, 10.0, 5.0, 2.0, 1.0) AS score
                    FROM jobs_fts WHERE jobs_fts MATCH ?)
                SELECT hits.id FROM hits
                WHERE hits.id IN (SELECT j.id FROM jobs j {_RELEVANCE_JOIN} WHERE {where})
                ORDER BY hits.score ASC, hits.id DESC LIMIT ? OFFSET ?""",
            [match, *params, page_size, offset],
        ).fetchall()
    else:
        rows = connection.execute(
            f"SELECT j.id FROM jobs j {_RELEVANCE_JOIN} WHERE {where}{text_clause} "
            f"ORDER BY {_ORDER_BY[applied_sort]} LIMIT ? OFFSET ?",
            [*params, *text_params, page_size, offset],
        ).fetchall()
    return int(total), [int(row[0]) for row in rows], applied_sort


_SUMMARY_COLUMNS = f"""
    j.id, j.title, j.company_name, j.location_raw, j.city, j.country, j.is_remote,
    j.experience_level, j.employment_type AS employment_type_raw, ({EFFECTIVE_TYPE_SQL}) AS employment_type,
    j.posted_at, j.first_seen_at, {LISTED_AT_SQL} AS listed_at, j.needs_description, j.status,
    a.id AS application_id, a.stage AS stage,
    r.final_score AS final_score, r.gate_status AS gate_status, r.role_family AS role_family,
    r.best_profile AS best_profile, r.llm_status AS llm_status, r.gate_reason AS gate_reason,
    r.seniority_level AS seniority_level, r.seniority_evidence AS seniority_evidence,
    r.location_fit AS location_fit, r.location_reason AS location_reason, r.location_matched AS location_matched,
    j.workplace_type AS workplace_type, j.remote_scope AS remote_scope,
    j.remote_eligible_india AS remote_eligible_india, j.work_auth_required AS work_auth_required,
    (SELECT f.action FROM job_feedback_current f WHERE f.job_id = j.id) AS feedback,
    (SELECT d.action FROM job_decision_current d WHERE d.job_id = j.id) AS decision,
    (SELECT d.reason FROM job_decision_current d WHERE d.job_id = j.id) AS decision_reason,
    r.eligibility_json AS eligibility_json, r.learned_json AS learned_json,
    (SELECT fr.band FROM job_freshness fr WHERE fr.job_id = j.id) AS freshness_band,
    (SELECT fr.age_days FROM job_freshness fr WHERE fr.job_id = j.id) AS age_days,
    (SELECT h.reason FROM job_auto_hidden h WHERE h.job_id = j.id AND h.restored_at IS NULL) AS auto_hidden_reason,
    (SELECT GROUP_CONCAT(source) FROM
        (SELECT DISTINCT s.source AS source FROM job_sightings s WHERE s.job_id = j.id ORDER BY s.source)) AS sources
"""


def fetch_summaries(connection: sqlite3.Connection, job_ids: list[int]) -> list[dict[str, object]]:
    """Summary rows for `job_ids`, in the order given."""
    if not job_ids:
        return []
    rows = connection.execute(
        f"""SELECT {_SUMMARY_COLUMNS}
            FROM jobs j LEFT JOIN applications a ON a.job_id = j.id {_RELEVANCE_JOIN}
            WHERE j.id IN ({",".join("?" for _ in job_ids)})""",
        job_ids,
    ).fetchall()
    by_id = {int(row["id"]): summary_from_row(row) for row in rows}
    cautions = signals.compute_signals(connection, [i for i in job_ids if i in by_id], utc_now())
    for job_id, item in by_id.items():
        item["has_caution_signal"] = signals.has_caution(cautions.get(job_id, []))
        item["applied_company_warning"] = decisions.company_warning(connection, job_id, str(item["company_name"]))
    return [by_id[job_id] for job_id in job_ids if job_id in by_id]


def summary_from_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "title": row["title"],
        "company_name": row["company_name"],
        "location": row["location_raw"],
        "city": row["city"],
        "country": row["country"],
        "is_remote": None if row["is_remote"] is None else bool(row["is_remote"]),
        "experience_level": row["experience_level"],
        "employment_type": row["employment_type"],
        "employment_type_raw": row["employment_type_raw"],
        "posted_at": row["posted_at"],
        "first_seen_at": row["first_seen_at"],
        "listed_at": row["listed_at"],
        "sources": (row["sources"] or "").split(",") if row["sources"] else [],
        "needs_description": bool(row["needs_description"]),
        "status": row["status"],
        "saved": row["application_id"] is not None,
        "application_id": row["application_id"],
        "stage": row["stage"],
        "match_score": row["final_score"],
        "gate_status": row["gate_status"],
        "role_family": row["role_family"],
        "best_profile": row["best_profile"],
        "llm_scored": row["llm_status"] == "scored",
        "workplace_type": row["workplace_type"] or "unknown",
        "remote_scope": row["remote_scope"] or "unknown",
        "remote_eligible_india": None if row["remote_eligible_india"] is None else bool(row["remote_eligible_india"]),
        "work_auth_required": bool(row["work_auth_required"]),
        "feedback": row["feedback"],
        "decision": row["decision"],
        "decision_reason": row["decision_reason"],
        "likely_ineligible": likely_ineligible_note(_json_or(row["eligibility_json"], [])) or None,
        "learned_rules": [{"id": r.get("id"), "phrase": r.get("phrase"), "tooltip": r.get("tooltip")}
                          for r in _json_or(row["learned_json"], []) if isinstance(r, dict)],
        "seniority_level": row["seniority_level"],
        "seniority_evidence": row["seniority_evidence"],
        "location_fit": row["location_fit"],
        "location_reason": row["location_reason"],
        "location_matched": row["location_matched"],
        "exclusion_reason": row["gate_reason"] if row["gate_status"] == "excluded" else None,
        "freshness": row["freshness_band"] or "unknown",
        "age_days": row["age_days"],
        "auto_hidden": row["auto_hidden_reason"],
    }


def fetch_job_detail(connection: sqlite3.Connection, job_id: int) -> dict[str, object] | None:
    row = connection.execute(
        f"""SELECT {_SUMMARY_COLUMNS},
                   j.description_text, j.canonical_apply_url, j.salary_min, j.salary_max, j.salary_currency,
                   j.sponsorship_note, j.graduation_year_hint, j.last_seen_at, j.closed_at, j.closed_reason, j.company_id,
                   r.llm_reason AS llm_reason, r.llm_score AS llm_score, r.llm_flags AS llm_flags,
                   r.score_components AS score_components, r.similarity_ai AS similarity_ai,
                   r.similarity_fullstack AS similarity_fullstack, r.similarity_percentile AS similarity_percentile,
                   r.gate_rule AS gate_rule, r.gate_reason AS gate_reason, r.required_years AS required_years,
                   j.remote_evidence AS remote_evidence, j.remote_regions AS remote_regions,
                   j.remote_source AS remote_source
            FROM jobs j LEFT JOIN applications a ON a.job_id = j.id {_RELEVANCE_JOIN} WHERE j.id = ?""",
        (job_id,),
    ).fetchone()
    if row is None:
        return None

    sightings = connection.execute(
        """SELECT source, source_job_id, source_url, attribution_text, first_seen_at, last_seen_at
           FROM job_sightings WHERE job_id = ? ORDER BY first_seen_at, id""",
        (job_id,),
    ).fetchall()
    sighting_out = [
        {
            "source": s["source"], "source_job_id": s["source_job_id"], "url": safe_http_url(s["source_url"]),
            "attribution_text": s["attribution_text"], "first_seen_at": s["first_seen_at"],
            "last_seen_at": s["last_seen_at"],
        }
        for s in sightings
    ]
    # Prefer the employer's own ATS page, then a manual link, then whatever a feed or email gave us.
    ranked = sorted(
        (s for s in sighting_out if s["url"]),
        key=lambda s: _ATS_PRIORITY.get(str(s["source"]), 2),
    )
    apply_url = ranked[0]["url"] if ranked else safe_http_url(row["canonical_apply_url"])

    company = None
    if row["company_id"] is not None:
        company_row = connection.execute(
            "SELECT id, name, ats_type, slug, status FROM companies WHERE id = ?", (row["company_id"],)
        ).fetchone()
        company = dict(company_row) if company_row else None

    application = None
    if row["application_id"] is not None:
        application = fetch_application(connection, int(row["application_id"]))

    liveness_row = connection.execute(
        "SELECT result, detail, http_status, method, trigger, checked_at FROM job_liveness WHERE job_id = ?", (job_id,)
    ).fetchone()
    detail = summary_from_row(row)
    detail.update(
        {
            "liveness": dict(liveness_row) if liveness_row else None,
            "closed_reason": row["closed_reason"],
            "remember_level_word": level_word_in_title(str(row["title"])),
            "description_text": row["description_text"],
            "apply_url": apply_url,
            "sightings": sighting_out,
            "company": company,
            "application": application,
            "salary_min": row["salary_min"],
            "salary_max": row["salary_max"],
            "salary_currency": row["salary_currency"],
            "sponsorship_note": row["sponsorship_note"],
            "graduation_year_hint": row["graduation_year_hint"],
            "last_seen_at": row["last_seen_at"],
            "closed_at": row["closed_at"],
            "match": _match_detail(row),
            "eligibility": _eligibility_chips(row),
            "why_match": _why_match(row),
            "signals": signals.compute_signals(connection, [job_id], utc_now()).get(job_id, []),
        }
    )
    detail["has_caution_signal"] = signals.has_caution(detail["signals"])  # type: ignore[arg-type]
    detail["applied_company_warning"] = decisions.company_warning(connection, job_id, str(row["company_name"]))
    return detail


def _json_or(value: object, default: object) -> object:
    if not value:
        return default
    try:
        return json.loads(str(value))
    except ValueError:
        return default


_FORMULA_LEGEND = ("score = 55% fit + 25% similarity rank + 20% location, then small adjustments "
                   "(junior role boost, years penalty, learned rules); capped at 50 when the posting requires a PhD, "
                   "Master's or citizenship")


def _eligibility_chips(row: sqlite3.Row) -> list[dict[str, object]]:
    """Flags for the job panel. US work-authorization chips are hidden for jobs the location policy shows as open to India."""
    open_to_india = row["location_fit"] in ("india", "remote_open_to_india")
    out = []
    for flag in _json_or(row["eligibility_json"], []):
        if not isinstance(flag, dict) or (open_to_india and flag.get("kind") in INFO_ONLY_KINDS):
            continue
        out.append({"kind": flag.get("kind"), "strength": flag.get("strength"),
                    "label": FLAG_LABELS.get(str(flag.get("kind")), str(flag.get("kind"))),
                    "snippet": str(flag.get("snippet", ""))[:120]})
    return out


def _why_match(row: sqlite3.Row) -> dict[str, object]:
    """Everything needed to read the score at a glance (all stored values; nothing computed with a model)."""
    components = _json_or(row["score_components"], {})
    components = components if isinstance(components, dict) else {}
    ai, full = row["similarity_ai"], row["similarity_fullstack"]
    note = None
    if ai is not None and full is not None:
        winner = "Full-stack" if full >= ai else "AI"
        gap = abs(full - ai)
        note = (f"{winner} profile won on similarity ({max(ai, full):.3f} vs {min(ai, full):.3f}, difference {gap:.3f})"
                + ("; that is a near tie" if gap < 0.02 else ""))
        if row["role_family"] == "ai_ml" and winner == "Full-stack":
            note += ". The title says AI/ML, so the AI resume is the better base unless you prefer otherwise"
    fit_source = components.get("fit_source")
    reason = row["llm_reason"]
    return {
        "fit_reason": reason, "fit": components.get("fit"), "fit_source": fit_source,
        "fit_source_note": {"llm": "Gemini scored this job", "ranker": "estimated by your local ranker, no Gemini score yet",
                            "similarity": "raw resume similarity (no score yet)",
                            "classified_irrelevant": "Gemini triage said not relevant"}.get(str(fit_source), None),
        "similarity_ai": ai, "similarity_fullstack": full, "best_profile": row["best_profile"], "profile_note": note,
        "formula": _FORMULA_LEGEND, "components": components,
    }


def _match_detail(row: sqlite3.Row) -> dict[str, object] | None:
    if row["gate_status"] is None and row["final_score"] is None:
        return None
    flags = _json_or(row["llm_flags"], {})
    scored = flags.get("scored", {}) if isinstance(flags, dict) else {}
    components = _json_or(row["score_components"], {})
    regions = _json_or(row["remote_regions"], [])
    return {
        "final_score": row["final_score"],
        "llm_score": row["llm_score"],
        "reason": row["llm_reason"],
        "matched_skills": list(scored.get("matched_skills", [])),
        "missing_skills": list(scored.get("missing_skills", [])),
        "red_flags": list(scored.get("red_flags", [])),
        "recommended_profile": scored.get("recommended_profile") or row["best_profile"],
        "similarity_ai": row["similarity_ai"],
        "similarity_fullstack": row["similarity_fullstack"],
        "similarity_percentile": row["similarity_percentile"],
        "components": components if isinstance(components, dict) else {},
        "gate_status": row["gate_status"],
        "gate_rule": row["gate_rule"],
        "exclusion_reason": row["gate_reason"] if row["gate_status"] == "excluded" else None,
        "required_years": row["required_years"],
        "remote_evidence": row["remote_evidence"],
        "remote_regions": [str(item) for item in regions] if isinstance(regions, list) else [],
        "remote_source": row["remote_source"],
    }


def fetch_application(connection: sqlite3.Connection, application_id: int) -> dict[str, object] | None:
    row = connection.execute(
        """SELECT id, job_id, stage, applied_at, notes, follow_up_at, created_at, updated_at, tailored_resume_id,
                  contact_name, contact_email, contact_phone,
                  (SELECT t.resume_number FROM tailored_resumes t WHERE t.id = applications.tailored_resume_id) AS resume_number
           FROM applications WHERE id = ?""",
        (application_id,),
    ).fetchone()
    return dict(row) if row else None


def fetch_board(connection: sqlite3.Connection) -> dict[str, object]:
    rows = connection.execute(
        """SELECT a.id, a.job_id, a.stage, a.applied_at, a.notes, a.follow_up_at, a.created_at, a.updated_at,
                  a.tailored_resume_id, a.contact_name, a.contact_email, a.contact_phone,
                  (SELECT t.resume_number FROM tailored_resumes t WHERE t.id = a.tailored_resume_id) AS resume_number,
                  j.title, j.company_name, j.location_raw, j.country, j.status AS job_status, j.canonical_apply_url,
                  (SELECT s.source_url FROM job_sightings s WHERE s.job_id = j.id
                   ORDER BY CASE WHEN s.source IN ('greenhouse','lever','ashby','workable','smartrecruiters',
                                                   'recruitee','workday','oracle','amazon_jobs','manual') THEN 0 ELSE 1 END, s.id LIMIT 1) AS sighting_url
           FROM applications a CROSS JOIN jobs j ON j.id = a.job_id
           ORDER BY a.updated_at DESC, a.id DESC"""
    ).fetchall()
    columns: dict[str, list[dict[str, object]]] = {stage: [] for stage in STAGES}
    for row in rows:
        columns.setdefault(row["stage"], []).append(
            {
                "application": {
                    key: row[key]
                    for key in ("id", "job_id", "stage", "applied_at", "notes", "follow_up_at", "created_at", "updated_at",
                                "tailored_resume_id", "resume_number", "contact_name", "contact_email", "contact_phone")
                },
                "title": row["title"],
                "company_name": row["company_name"],
                "location": row["location_raw"],
                "country": row["country"],
                "apply_url": safe_http_url(row["sighting_url"]) or safe_http_url(row["canonical_apply_url"]),
                "job_status": row["job_status"],
            }
        )
    return {
        "total": len(rows),
        "columns": [{"stage": stage, "count": len(columns[stage]), "cards": columns[stage]} for stage in STAGES],
    }


def compute_facets(connection: sqlite3.Connection, now: datetime) -> dict[str, object]:
    def pairs(sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        return [{"value": str(row[0]), "count": int(row[1])} for row in connection.execute(sql, params)]

    total_open = connection.execute("SELECT COUNT(*) FROM jobs WHERE status = 'open'").fetchone()[0]
    return {
        "total_open": int(total_open),
        "levels": pairs(
            "SELECT experience_level, COUNT(*) FROM jobs WHERE status = 'open' GROUP BY 1 ORDER BY 2 DESC"
        ),
        "employment_types": pairs(
            f"SELECT {EFFECTIVE_TYPE_SQL} AS t, COUNT(*) FROM jobs j WHERE j.status = 'open' GROUP BY 1 ORDER BY 2 DESC"
        ),
        "countries": pairs(
            "SELECT COALESCE(country, 'unknown'), COUNT(*) FROM jobs WHERE status = 'open' "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 60"
        ),
        "sources": pairs(
            """SELECT s.source, COUNT(DISTINCT s.job_id) FROM job_sightings s
               WHERE s.job_id IN (SELECT id FROM jobs WHERE status = 'open')
               GROUP BY s.source ORDER BY 2 DESC"""
        ),
        "remote": int(connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'open' AND is_remote = 1").fetchone()[0]),
        "needs_description": int(connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'open' AND needs_description = 1").fetchone()[0]),
        "new_24h": int(connection.execute(
            f"SELECT COUNT(*) FROM jobs j WHERE j.status = 'open' AND {LISTED_AT_SQL} >= ?",
            (to_iso(now - timedelta(hours=24)),),
        ).fetchone()[0]),
        "generated_at": to_iso(now),
        "auto_hidden": int(connection.execute(
            "SELECT COUNT(*) FROM job_auto_hidden WHERE restored_at IS NULL").fetchone()[0]),
        "category_counts": _category_counts(connection, now),
        **_relevance_facets(connection),
    }


def _category_counts(connection: sqlite3.Connection, now: datetime) -> list[dict[str, object]]:
    """How many jobs each category shows right now (the same WHERE the list uses, so the numbers always agree)."""
    out = []
    for category in CATEGORIES:
        where, params = build_where(JobFilters(category=category), now)
        count = connection.execute(f"SELECT COUNT(*) FROM jobs j {_RELEVANCE_JOIN} WHERE {where}", params).fetchone()[0]
        out.append({"value": category, "count": int(count)})
    return out


def _relevance_facets(connection: sqlite3.Connection) -> dict[str, object]:
    def scalar(sql: str) -> int:
        return int(connection.execute(sql).fetchone()[0])

    open_join = "FROM jobs j LEFT JOIN job_relevance r ON r.job_id = j.id WHERE j.status = 'open'"
    visible = "(r.gate_status IS NULL OR r.gate_status <> 'excluded')"
    not_dismissed = ("NOT EXISTS (SELECT 1 FROM job_feedback_current f WHERE f.job_id = j.id AND f.action = 'dismiss') "
                     f"AND NOT {ACTIVE_HIDDEN_SQL}")
    locations = [
        {"value": str(row[0]), "count": int(row[1])}
        for row in connection.execute(
            f"SELECT COALESCE(r.location_fit, 'unknown'), COUNT(*) {open_join} AND {visible} AND {not_dismissed} "
            "GROUP BY 1 ORDER BY 2 DESC")
    ]
    bands = [
        {"value": str(row[0]), "count": int(row[1])}
        for row in connection.execute(
            f"""SELECT COALESCE(fr.band, 'unknown'), COUNT(*)
                FROM jobs j LEFT JOIN job_relevance r ON r.job_id = j.id LEFT JOIN job_freshness fr ON fr.job_id = j.id
                WHERE j.status = 'open' AND {visible} AND {not_dismissed} AND NOT {ACTIVE_HIDDEN_SQL}
                GROUP BY 1 ORDER BY 2 DESC""")
    ]
    return {
        "freshness_bands": bands,
        "location_fits": locations,
        "scored": scalar(f"SELECT COUNT(*) {open_join} AND r.final_score IS NOT NULL AND {visible}"),
        "relevant": scalar(f"SELECT COUNT(*) {open_join} AND {visible} AND {not_dismissed}"),
        "filtered_out": scalar(f"SELECT COUNT(*) {open_join} AND r.gate_status = 'excluded'"),
        "workplace_types": [
            {"value": str(row[0]), "count": int(row[1])}
            for row in connection.execute(
                f"SELECT j.workplace_type, COUNT(*) {open_join} AND {visible} AND {not_dismissed} GROUP BY 1 ORDER BY 2 DESC")
        ],
        "remote_open_to_india": scalar(
            f"SELECT COUNT(*) {open_join} AND {visible} AND {not_dismissed} AND j.remote_eligible_india = 1"),
        "gate_rules": [
            {"value": str(row[0]), "count": int(row[1])}
            for row in connection.execute(
                f"SELECT r.gate_rule, COUNT(*) {open_join} AND r.gate_status = 'excluded' GROUP BY 1 ORDER BY 2 DESC")
        ],
    }
