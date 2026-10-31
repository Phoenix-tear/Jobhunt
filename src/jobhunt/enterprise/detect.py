"""`detect-ats`: find out which career platform each enterprise company runs (public landing page only)."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

from jobhunt import companies as company_repo
from jobhunt.enterprise.polite import HostBlocked, PoliteFetcher
from jobhunt.enterprise.registry import Detection, detect_from_urls
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)

# Platforms the existing board adapters already poll: they go into the `companies` table.
SUPPORTED_BOARDS = frozenset({"greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee"})
# Platforms an adapter exists for in this build.
ADAPTERS = frozenset({"workday", "eightfold", "phenom", "oracle"})
MAX_PAGE_CHARS = 400_000


@dataclass
class DetectSummary:
    checked: int = 0
    by_platform: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    added_to_companies: int = 0
    blocked_hosts: dict[str, str] = field(default_factory=dict)


def detect_all(
    connection: sqlite3.Connection, fetcher: PoliteFetcher, *, only: list[str] | None = None,
    limit: int | None = None, clock: Clock = utc_now,
) -> DetectSummary:
    summary = DetectSummary()
    rows = connection.execute(
        "SELECT id, name, careers_url FROM enterprise_companies "
        "WHERE COALESCE(evidence, '') NOT LIKE 'set by hand%' ORDER BY id").fetchall()   # hand-pinned rows stay
    wanted = {name.lower() for name in only} if only else None
    for row in rows:
        if wanted and row["name"].lower() not in wanted:
            continue
        if limit is not None and summary.checked >= limit:
            break
        summary.checked += 1
        platform, params, status, evidence, error = _detect_one(fetcher, row["careers_url"])
        now = to_iso(clock())
        connection.execute(
            """UPDATE enterprise_companies SET detected_ats = ?, adapter_params = ?, status = ?, evidence = ?,
                      last_checked_at = ?, last_error = ? WHERE id = ?""",
            (platform, json.dumps(params) if params else None, status, evidence[:500], now, error, row["id"]),
        )
        if platform in SUPPORTED_BOARDS and params.get("slug"):
            summary.added_to_companies += company_repo.add_discovered_companies(
                connection,
                [company_repo.DiscoveredCompany(ats_type=platform, slug=str(params["slug"]), name=row["name"],
                                                region=params.get("region"), discovered_via="enterprise:detect-ats")],
                clock(),
            )
        connection.commit()
        summary.by_platform[platform] = summary.by_platform.get(platform, 0) + 1
        summary.by_status[status] = summary.by_status.get(status, 0) + 1
        logger.info("%s: %s (%s)", row["name"], platform, status)
    summary.blocked_hosts = fetcher.blocked
    return summary


def _detect_one(fetcher: PoliteFetcher, url: str) -> tuple[str, dict, str, str, str | None]:
    """(platform, params, status, evidence, error)."""
    chain = [url]
    try:
        response = fetcher.get(url)
        for _ in range(6):
            if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                nxt = urljoin(chain[-1], response.headers["location"])
                chain.append(nxt)
                response = fetcher.get(nxt)
                continue
            break
    except HostBlocked as error:
        status = "blocked" if "robots" not in error.reason and "network" not in error.reason else "unreachable"
        return "unknown", {}, status, f"{error.reason}; chain: {' -> '.join(chain)[:300]}", error.reason
    except httpx.HTTPError as error:
        return "unknown", {}, "unreachable", f"chain: {' -> '.join(chain)[:300]}", type(error).__name__
    if response.status_code >= 400:
        return "unknown", {}, "unreachable", f"HTTP {response.status_code}; chain: {' -> '.join(chain)[:300]}", \
            f"HTTP {response.status_code}"
    text = response.text[:MAX_PAGE_CHARS] if "html" in response.headers.get("content-type", "html").lower() else ""
    detection = detect_from_urls(chain, text)
    if detection.platform in {"eightfold", "phenom", "oracle"} and not any(
            detection.params.get(key) for key in ("domain", "locale", "site")):
        detection = _enrich(fetcher, detection, chain, text)
    if detection.platform == "unknown":
        return "unknown", {}, "needs_adapter", f"{detection.evidence}; chain: {' -> '.join(chain)[:300]}", None
    status = "detected" if (detection.platform in ADAPTERS and detection.params) or \
        (detection.platform in SUPPORTED_BOARDS and detection.params.get("slug")) else "needs_adapter"
    return detection.platform, detection.params, status, detection.evidence, None


_EIGHTFOLD_HOST = re.compile(r"([a-z0-9-]+)\.eightfold\.ai", re.IGNORECASE)
_ORACLE_HOST = re.compile(r"([a-z0-9-]+\.fa\.[a-z0-9.-]*oraclecloud\.com)", re.IGNORECASE)
_ORACLE_SITE = re.compile(r"/sites/([A-Za-z0-9_]+)")


def _enrich(fetcher: PoliteFetcher, detection: Detection, chain: list[str], text: str) -> Detection:
    """Find the adapter parameters for platforms whose landing page only names the platform."""
    haystack = " ".join(chain) + " " + text
    try:
        if detection.platform == "eightfold":
            hosts = [f"{m.group(1)}.eightfold.ai" for m in _EIGHTFOLD_HOST.finditer(haystack)
                     if m.group(1).lower() not in {"app", "www"}]
            for host in dict.fromkeys(hosts):
                page = fetcher.get(f"https://{host}/careers", follow=3)
                domain = (re.search(r'"domain"\s*:\s*"([a-z0-9.\-]+)"', page.text)
                          or re.search(r"domain=([a-z0-9.\-]+)", page.text)) if page.status_code == 200 else None
                if domain:
                    return Detection("eightfold", {"host": host, "domain": domain.group(1)},
                                     f"Eightfold host {host}, domain {domain.group(1)}")
        elif detection.platform == "phenom":
            final = urlsplit(chain[-1])
            host = final.netloc
            locale = re.search(r"/([a-z]{2}/[a-z]{2})(?:/|$)", final.path)
            for candidate in ([locale.group(1)] if locale else []) + ["us/en", "en"]:
                page = fetcher.get(f"https://{host}/{candidate}/search-results", follow=3)
                if page.status_code == 200 and '"refNum"' in page.text:
                    return Detection("phenom", {"host": host, "locale": candidate}, f"Phenom search page {host}/{candidate}")
        elif detection.platform == "oracle":
            host = _ORACLE_HOST.search(haystack)
            sites = [s for s in _ORACLE_SITE.findall(haystack) if s.upper().startswith("CX") or "Career" in s]
            if host and sites:
                site = sorted(sites, key=lambda s: (not s.upper().startswith("CX"), len(s)))[0]
                return Detection("oracle", {"host": host.group(1).lower(), "site": site}, f"Oracle {host.group(1)} {site}")
    except (HostBlocked, httpx.HTTPError):
        return Detection(detection.platform, {}, detection.evidence + "; parameters could not be fetched")
    return Detection(detection.platform, {}, detection.evidence + "; parameters not found")
