""""Is this posting still open?" -- one polite request per job, three possible answers (no model).

* `open`       the posting answered normally;
* `closed`     404 / 410, "no longer available / position filled / expired" wording, a redirect away from the posting, or
               (Ashby / Workable boards) an id missing from a board that listed other jobs;
* `uncertain`  403 / 429 / 5xx, a bot challenge, a timeout, robots.txt, an unreadable answer. NEVER marks a job closed.

Greenhouse, Lever, SmartRecruiters and Recruitee have a public single-job endpoint (the adapters' own APIs); Ashby and
Workable only publish the board listing, which is read once and searched for the id. Any other host gets a plain GET of
the apply URL. All requests go through `PoliteFetcher` (robots.txt, per-host rate limit, challenge detection, redirects
re-checked). A `closed` answer closes the job locally (reason `closed_checked`) and moves it to Auto-hidden;
`open` on a job that was auto-hidden as closed restores it.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

from jobhunt import freshness, lifecycle
from jobhunt.config import Config, LivenessSettings
from jobhunt.enterprise.polite import HostBlocked, PoliteFetcher
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)

OPEN, CLOSED, UNCERTAIN = "open", "closed", "uncertain"

_CLOSED_TEXT = re.compile(
    r"no longer (?:available|accepting|open|active|listed|taking)|"
    r"(?:position|role|job|vacancy|posting|opportunity)\s+(?:has\s+been\s+|is\s+|was\s+)?"
    r"(?:filled|closed|removed|taken\s+down|no\s+longer\s+(?:available|open|active))|"
    r"(?:this\s+)?(?:job|position|role|posting)\s+(?:has\s+)?expired|job\s+expired|posting\s+expired|"
    r"(?:job|position|posting|page)\s+(?:was\s+)?not\s+found|job\s+does\s+not\s+exist|"
    r"(?:no\s+longer\s+exists|has\s+been\s+(?:filled|closed|removed|taken\s+down))|"
    r"applications?\s+(?:for\s+this\s+(?:job|role|position)\s+)?(?:are\s+)?closed",
    re.IGNORECASE)
_GENERIC_PATHS = {"", "/", "/careers", "/careers/", "/jobs", "/jobs/", "/en", "/en/careers", "/search", "/careers/search",
                  "/job-search", "/en/jobs", "/open-positions", "/join-us", "/home"}
_API_SOURCES = ("greenhouse", "lever", "smartrecruiters", "recruitee", "ashby", "workable")


class Fetcher(Protocol):
    request_count: int

    def get(self, url: str, *, follow: int = 0, headers: Any = None) -> httpx.Response: ...


@dataclass(frozen=True)
class LivenessResult:
    result: str                       # open | closed | uncertain
    detail: str
    http_status: int | None = None
    method: str = "page"              # ats_api | page


@dataclass(frozen=True)
class Target:
    job_id: int
    title: str
    status: str
    apply_url: str | None
    sightings: tuple[tuple[str, str, str | None], ...] = ()      # (source, source_job_id, url)
    ats_type: str | None = None
    slug: str | None = None
    region: str | None = None          # Lever: eu | us


# --- classification (pure) --------------------------------------------------------------------------------------------


def _normal_path(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    return host, parts.path.rstrip("/").lower()


def moved_off_posting(original: str, final: str, token: str | None) -> bool:
    """True when following redirects took us away from the posting (a listing page, the home page, another site)."""
    if original == final:
        return False
    original_host, original_path = _normal_path(original)
    final_host, final_path = _normal_path(final)
    if (original_host, original_path) == (final_host, final_path):
        return False                               # http -> https, trailing slash, tracking parameters
    if final_host != original_host and not final_host.endswith("." + original_host.split(".", 1)[-1]):
        return True
    if (final_path or "/") in _GENERIC_PATHS or final_path + "/" in _GENERIC_PATHS:
        return True
    if token:
        return token.lower() not in final.lower()
    return len([part for part in final_path.split("/") if part]) < len([part for part in original_path.split("/") if part])


def classify_page(status: int, text: str, original_url: str, final_url: str, token: str | None) -> tuple[str, str]:
    """(result, detail) for a plain GET of a posting page."""
    if status in (404, 410):
        return CLOSED, f"HTTP {status}"
    if status in (401, 403, 407, 408, 425, 429) or status >= 500:
        return UNCERTAIN, f"HTTP {status}"
    if status >= 400:
        return UNCERTAIN, f"HTTP {status}"
    if moved_off_posting(original_url, final_url, token):
        return CLOSED, f"redirected away from the posting to {urlsplit(final_url).netloc}{urlsplit(final_url).path}"
    match = _CLOSED_TEXT.search(text[:20000]) if text else None
    if match:
        return CLOSED, f"page says: {match.group(0)[:80]}"
    return OPEN, f"HTTP {status}"


def classify_api(status: int) -> tuple[str, str]:
    """(result, detail) for a single-job API call."""
    if status in (404, 410):
        return CLOSED, f"HTTP {status} from the job API"
    if 200 <= status < 300:
        return OPEN, f"HTTP {status} from the job API"
    return UNCERTAIN, f"HTTP {status} from the job API"


# --- the checker ----------------------------------------------------------------------------------------------------------


def api_url(target: Target) -> tuple[str, str | None] | None:
    """(URL, board-membership id) of the adapter's public endpoint for this job; the id is set for list-only boards."""
    if not target.ats_type or not target.slug:
        return None
    for source, source_job_id, _ in target.sightings:
        if source != target.ats_type or not source_job_id:
            continue
        slug, job_id = quote(target.slug, safe=""), quote(str(source_job_id), safe="")
        if source == "greenhouse":
            return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}", None
        if source == "lever":
            host = "api.eu.lever.co" if target.region == "eu" else "api.lever.co"
            return f"https://{host}/v0/postings/{slug}/{job_id}", None
        if source == "smartrecruiters":
            return f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}", None
        if source == "recruitee" and re.fullmatch(r"[a-z0-9-]{1,63}", target.slug.lower()):
            return f"https://{target.slug.lower()}.recruitee.com/api/offers/{job_id}", None
        if source == "ashby":
            return f"https://api.ashbyhq.com/posting-api/job-board/{slug}", str(source_job_id)
        if source == "workable":
            return f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true", str(source_job_id)
    return None


def _board_contains(payload: Any, wanted: str) -> bool | None:
    """Is `wanted` among the jobs of a board listing? None when the listing is empty or unreadable."""
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list) or not jobs:
        return None
    for item in jobs:
        if isinstance(item, dict) and wanted in {str(item.get(key)) for key in ("id", "shortcode", "code") if item.get(key)}:
            return True
    return False


class LivenessChecker:
    def __init__(self, fetcher: Fetcher, settings: LivenessSettings) -> None:
        self.fetcher = fetcher
        self.settings = settings

    def check(self, target: Target) -> LivenessResult:
        try:
            return self._check(target)
        except HostBlocked as error:
            return LivenessResult(UNCERTAIN, f"not checked: {error.reason}", method="page")
        except (httpx.HTTPError, ValueError, OSError) as error:
            return LivenessResult(UNCERTAIN, f"not checked: {type(error).__name__}", method="page")
        except Exception as error:  # noqa: BLE001 - a check must never take the run down
            logger.warning("liveness check of job %s failed: %s", target.job_id, error)
            return LivenessResult(UNCERTAIN, f"not checked: {type(error).__name__}", method="page")

    def _check(self, target: Target) -> LivenessResult:
        endpoint = api_url(target)
        if endpoint is not None:
            url, wanted = endpoint
            response = self.fetcher.get(url)
            if wanted is None:
                result, detail = classify_api(response.status_code)
                return LivenessResult(result, detail, response.status_code, "ats_api")
            if not 200 <= response.status_code < 300:
                result, detail = classify_api(response.status_code)
                return LivenessResult(UNCERTAIN if result == CLOSED else result, detail, response.status_code, "ats_api")
            try:
                present = _board_contains(response.json(), wanted)
            except ValueError:
                return LivenessResult(UNCERTAIN, "the board listing was not JSON", response.status_code, "ats_api")
            if present is None:
                return LivenessResult(UNCERTAIN, "the board listing was empty or unreadable", response.status_code, "ats_api")
            if present:
                return LivenessResult(OPEN, "listed on the employer's board", response.status_code, "ats_api")
            return LivenessResult(CLOSED, "no longer listed on the employer's board", response.status_code, "ats_api")
        if not target.apply_url:
            return LivenessResult(UNCERTAIN, "no apply URL to check", None, "page")
        response = self.fetcher.get(target.apply_url, follow=self.settings.max_redirects)
        token = next((source_job_id for _, source_job_id, _ in target.sightings if source_job_id), None)
        result, detail = classify_page(response.status_code, response.text if response.status_code < 400 else "",
                                       target.apply_url, str(response.url), token)
        return LivenessResult(result, detail, response.status_code, "page")


def make_fetcher(config: Config, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> PoliteFetcher:
    settings = config.freshness.liveness
    return PoliteFetcher(config.user_agent, requests_per_minute=60.0 / max(0.5, settings.seconds_between_requests),
                         timeout=settings.timeout_seconds, max_retries=0, transport=transport, sleep=sleep)


# --- database side --------------------------------------------------------------------------------------------------------


def _safe_url(value: str | None) -> str | None:
    if not value or len(value) > 2048 or any(ord(char) < 33 for char in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    return value if parts.scheme in {"http", "https"} and parts.hostname else None


def load_target(connection: sqlite3.Connection, job_id: int) -> Target | None:
    job = connection.execute(
        """SELECT j.id, j.title, j.status, j.canonical_apply_url, c.ats_type, c.slug, c.region
           FROM jobs j LEFT JOIN companies c ON c.id = j.company_id WHERE j.id = ?""", (job_id,)).fetchone()
    if job is None:
        return None
    rows = connection.execute(
        "SELECT source, source_job_id, source_url FROM job_sightings WHERE job_id = ? ORDER BY first_seen_at, id", (job_id,)
    ).fetchall()
    sightings = tuple((str(r["source"]), str(r["source_job_id"]), r["source_url"]) for r in rows)
    ats_first = sorted(sightings, key=lambda item: 0 if item[0] in _API_SOURCES else 1)
    apply_url = next((url for _, _, raw in ats_first if (url := _safe_url(raw))), None) or _safe_url(job["canonical_apply_url"])
    return Target(job_id=int(job["id"]), title=str(job["title"]), status=str(job["status"]), apply_url=apply_url,
                  sightings=sightings, ats_type=job["ats_type"], slug=job["slug"], region=job["region"])


def record_result(connection: sqlite3.Connection, job_id: int, outcome: LivenessResult, *, trigger: str,
                  now_iso: str) -> None:
    connection.execute(
        """INSERT INTO job_liveness (job_id, result, detail, http_status, method, trigger, checked_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(job_id) DO UPDATE SET result = excluded.result, detail = excluded.detail,
               http_status = excluded.http_status, method = excluded.method, trigger = excluded.trigger,
               checked_at = excluded.checked_at""",
        (job_id, outcome.result, outcome.detail[:300], outcome.http_status, outcome.method, trigger, now_iso))


def apply_result(connection: sqlite3.Connection, job_id: int, outcome: LivenessResult, now_iso: str) -> str | None:
    """Act on an answer. `closed`: close locally and move to Auto-hidden. `open`: restore a job hidden as closed.
    `uncertain`: nothing. Returns what was done ("closed" | "restored" | None)."""
    if outcome.result == CLOSED:
        lifecycle.close_job(connection, job_id, now_iso, lifecycle.CLOSED_CHECKED)
        freshness.hide_job(connection, job_id, freshness.CLOSED_CHECKED, outcome.detail, now_iso)
        return "closed"
    if outcome.result == OPEN:
        row = connection.execute("SELECT reason, restored_at FROM job_auto_hidden WHERE job_id = ?", (job_id,)).fetchone()
        if row is not None and row["reason"] == freshness.CLOSED_CHECKED and row["restored_at"] is None:
            freshness.restore_job(connection, job_id, now_iso)
            return "restored"
    return None


def check_job(connection: sqlite3.Connection, checker: LivenessChecker, job_id: int, *, trigger: str = "button",
              clock: Clock = utc_now) -> dict[str, Any] | None:
    """Check one job now (the panel button) and store/act on the answer. None when the job does not exist."""
    target = load_target(connection, job_id)
    if target is None:
        return None
    outcome = checker.check(target)
    now_iso = to_iso(clock())
    connection.execute("BEGIN IMMEDIATE")
    try:
        record_result(connection, job_id, outcome, trigger=trigger, now_iso=now_iso)
        action = apply_result(connection, job_id, outcome, now_iso)
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
    return {"job_id": job_id, "result": outcome.result, "detail": outcome.detail, "http_status": outcome.http_status,
            "method": outcome.method, "checked_at": now_iso, "action": action}


@dataclass
class ShortlistStats:
    candidates: int = 0
    checked: int = 0
    cached: int = 0
    skipped_restored: int = 0
    open: int = 0
    closed: int = 0
    uncertain: int = 0
    requests: int = 0
    capped: bool = False
    errors: list[str] = field(default_factory=list)


def shortlist_candidates(connection: sqlite3.Connection, config: Config, today: date) -> list[int]:
    """Saved jobs and jobs with a tailored resume that are not applied yet, then the jobs that would enter today's
    Gemini pool (so a closed one costs no scoring call). Order = priority when the request cap bites."""
    from jobhunt.matching import scoring

    ids: list[int] = []
    for row in connection.execute(
            """SELECT DISTINCT j.id FROM jobs j WHERE j.status = 'open' AND (
                   EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id AND a.stage = 'saved')
                OR EXISTS (SELECT 1 FROM job_decision_current d WHERE d.job_id = j.id AND d.action = 'saved')
                OR (EXISTS (SELECT 1 FROM tailored_resumes t WHERE t.job_id = j.id AND t.status != 'failed')
                    AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id AND a.stage != 'saved')
                    AND NOT EXISTS (SELECT 1 FROM job_decision_current d WHERE d.job_id = j.id
                                    AND d.action IN ('applied', 'not_applying'))))
               ORDER BY j.id"""):
        ids.append(int(row[0]))
    ids.extend(scoring.pool_job_ids(connection, config, today))
    return list(dict.fromkeys(ids))


def run_shortlist_liveness(connection: sqlite3.Connection, config: Config, fetcher: Fetcher, *, clock: Clock = utc_now,
                           sleep: Callable[[float], None] = time.sleep) -> ShortlistStats:
    """The daily step at the start of scoring: at most `max_requests_per_run` requests, spaced, cached 24 hours.
    Closed jobs are closed locally and hidden (reason closed_checked). Never raises on network trouble."""
    settings = config.freshness.liveness
    stats = ShortlistStats()
    now = clock()
    checker = LivenessChecker(fetcher, settings)
    try:
        candidates = shortlist_candidates(connection, config, now.date())
    except sqlite3.Error as error:
        stats.errors.append(str(error))
        return stats
    stats.candidates = len(candidates)
    cache_cutoff = to_iso(now.replace(microsecond=0) - timedelta(hours=settings.cache_hours))
    started = fetcher.request_count
    reserve = settings.max_redirects + 2               # one check may need robots.txt plus several hops
    for job_id in candidates:
        fresh = connection.execute("SELECT checked_at FROM job_liveness WHERE job_id = ?", (job_id,)).fetchone()
        if fresh is not None and fresh["checked_at"] >= cache_cutoff:
            stats.cached += 1
            continue
        restored = connection.execute("SELECT 1 FROM job_auto_hidden WHERE job_id = ? AND restored_at IS NOT NULL",
                                      (job_id,)).fetchone()
        if restored is not None:
            stats.skipped_restored += 1
            continue
        if fetcher.request_count - started + reserve > settings.max_requests_per_run:
            stats.capped = True
            break
        if stats.checked:
            sleep(settings.seconds_between_requests)
        try:
            result = check_job(connection, checker, job_id, trigger="match", clock=clock)
        except sqlite3.Error as error:
            stats.errors.append(f"#{job_id}: {error}")
            continue
        stats.checked += 1
        if result is not None:
            setattr(stats, result["result"], getattr(stats, result["result"]) + 1)
    stats.requests = fetcher.request_count - started
    return stats
