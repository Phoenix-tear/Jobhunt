"""Recognize job links in alert emails and canonicalize them with string handling only.

Nothing here (or anywhere in `jobhunt.alerts.parsing`) performs a network request: links to
LinkedIn, Naukri, Internshala, Indeed and the like are never fetched, not even to follow a
tracking redirect. A link is either recognized by its URL shape (and rewritten to a stable
canonical form), or it is kept verbatim as the apply URL with `needs_description=1`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

PLATFORMS = (
    "linkedin", "naukri", "internshala", "indeed", "wellfound", "unstop", "foundit", "cutshort",
    "instahyre", "glassdoor",
)

# Hosts whose links may legitimately be job links of that platform (tracking subdomains included).
PLATFORM_HOSTS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com",),
    "naukri": ("naukri.com",),
    "internshala": ("internshala.com",),
    "indeed": ("indeed.com", "indeed.co.in", "indeed.co.uk", "indeedemail.com"),
    "wellfound": ("wellfound.com", "angel.co"),
    "unstop": ("unstop.com",),
    "foundit": ("foundit.in", "monsterindia.com"),
    "cutshort": ("cutshort.io",),
    "instahyre": ("instahyre.com",),
    "glassdoor": ("glassdoor.com", "glassdoor.co.in", "glassdoor.co.uk"),
}


@dataclass(frozen=True)
class JobLink:
    """A recognized job link: where it points (canonical) and its platform job id when derivable."""

    url: str
    job_id: str | None
    recognized: bool = True   # False = verbatim link on a platform host whose shape we do not know


def host_matches(host: str, domains: tuple[str, ...]) -> bool:
    host = host.lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def stable_id(canonical_url: str) -> str:
    """Deterministic fallback id for links without a platform job id."""
    return hashlib.sha1(canonical_url.encode("utf-8")).hexdigest()[:16]


def recognize(platform: str, url: str) -> JobLink | None:
    """Canonical job link for `platform`, or None when the URL is not (recognizably) a job page."""
    parts = _split(url)
    if parts is None:
        return None
    host, path, query = parts
    handler = _HANDLERS.get(platform)
    if handler is None or not host_matches(host, PLATFORM_HOSTS[platform]):
        return None
    return handler(host, path, query)


def _split(url: str) -> tuple[str, str, dict[str, list[str]]] | None:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    return parts.hostname.lower(), unquote(parts.path), parse_qs(parts.query)


def _first(query: dict[str, list[str]], *names: str) -> str | None:
    for name in names:
        for key, values in query.items():
            if key.lower() == name.lower() and values and values[0]:
                return values[0]
    return None


def _linkedin(host: str, path: str, query: dict[str, list[str]]) -> JobLink | None:
    # /jobs/view/3912345678, /comm/jobs/view/3912345678, /jobs/view/software-engineer-at-acme-3912345678
    match = re.search(r"/jobs/view/(?:[^/?]*?-)?(\d{6,})/?$", path) or re.search(r"/jobs/view/(\d{6,})", path)
    job_id = match.group(1) if match else None
    if job_id is None:  # /jobs/collections/recommended/?currentJobId=3912345678
        current = _first(query, "currentJobId")
        job_id = current if current and current.isdigit() else None
    if job_id is None:
        return None
    return JobLink(f"https://www.linkedin.com/jobs/view/{job_id}", job_id)


def _indeed(host: str, path: str, query: dict[str, list[str]]) -> JobLink | None:
    job_key = _first(query, "jk", "vjk")
    if not job_key or not re.fullmatch(r"[0-9a-fA-F]{8,32}", job_key):
        return None
    base_host = host if host.endswith(("indeed.com", "indeed.co.in", "indeed.co.uk")) else "www.indeed.com"
    return JobLink(f"https://{base_host}/viewjob?jk={job_key}", job_key)


def _naukri(host: str, path: str, query: dict[str, list[str]]) -> JobLink | None:
    match = re.search(r"/job-listings-[^/?]*?-(\d{6,})/?$", path)
    if not match:
        return None
    return JobLink(f"https://www.naukri.com{path.rstrip('/')}", match.group(1))


def _slug_with_trailing_id(prefix_pattern: str, canonical_host: str):
    """Factory for `/<kind>/<slug>-<digits>` style links (Unstop, Foundit, Cutshort, Wellfound...)."""
    compiled = re.compile(prefix_pattern)

    def handler(host: str, path: str, query: dict[str, list[str]]) -> JobLink | None:
        match = compiled.search(path)
        if not match:
            return None
        return JobLink(f"https://{canonical_host}{match.group('path')}", match.group("id"))

    return handler


def _internshala(host: str, path: str, query: dict[str, list[str]]) -> JobLink | None:
    match = re.search(r"^(/(?:internship|job|fresher-job|work-from-home-job)s?/detail/[^/?#]+)/?$", path)
    if not match:
        return None
    detail = match.group(1)
    trailing = re.search(r"(\d{5,})$", detail)
    return JobLink(f"https://internshala.com{detail}", trailing.group(1) if trailing else stable_id(detail))


def _instahyre(host: str, path: str, query: dict[str, list[str]]) -> JobLink | None:
    match = re.search(r"^/(?:job-(\d+)-[^/?#]*|jobs/(\d+))/?", path)
    if not match:
        return None
    job_id = match.group(1) or match.group(2)
    return JobLink(f"https://www.instahyre.com{path.rstrip('/')}/", job_id)


def _glassdoor(host: str, path: str, query: dict[str, list[str]]) -> JobLink | None:
    listing_id = _first(query, "jl", "jobListingId")
    if not listing_id or not listing_id.isdigit():
        return None
    return JobLink(f"https://www.glassdoor.com/job-listing/?jl={listing_id}", listing_id)


_HANDLERS = {
    "linkedin": _linkedin,
    "indeed": _indeed,
    "naukri": _naukri,
    "internshala": _internshala,
    "instahyre": _instahyre,
    "glassdoor": _glassdoor,
    "wellfound": _slug_with_trailing_id(
        r"^(?:/company/[^/]+)?(?P<path>/jobs/(?P<id>\d{4,})(?:-[^/?#]*)?)/?$", "wellfound.com"
    ),
    "unstop": _slug_with_trailing_id(
        r"^(?P<path>/(?:internships|jobs|o)/[^/?#]*?-(?P<id>\d{4,}))/?$", "unstop.com"
    ),
    "foundit": _slug_with_trailing_id(
        r"^(?P<path>/job/[^/?#]*?-(?P<id>\d{5,}))/?$", "www.foundit.in"
    ),
    "cutshort": _slug_with_trailing_id(
        r"^(?P<path>/job/[^/?#]*?(?:-|/)(?P<id>[0-9a-zA-Z]{6,}))/?$", "cutshort.io"
    ),
}
