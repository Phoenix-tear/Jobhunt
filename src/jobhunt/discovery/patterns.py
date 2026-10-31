"""Recognize ATS-hosted URLs and pull out the board slug (and job id when present).

Pure string handling: no network, which is also what keeps phase 4's email links safe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from jobhunt.models import AtsType

_SLUG_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_UUID_LIKE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{15,}$")

# Path segments / subdomains that are part of an ATS's own site, never a customer slug.
RESERVED_SLUGS = frozenset(
    {
        "embed", "api", "v0", "v1", "v2", "jobs", "job", "j", "www", "static", "assets", "cdn", "app",
        "apply", "careers", "career", "login", "signin", "signup", "help", "support", "docs", "blog",
        "robots.txt", "sitemap.xml", "favicon.ico", "_next", "oauth", "oneclick-ui", "company",
        "about", "pricing", "customers", "resources", "legal", "privacy", "terms", "status", "public",
    }
)

_GREENHOUSE_HOSTS = {
    "boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io",
}


@dataclass(frozen=True)
class AtsReference:
    """A board (always) and optionally one job on it."""

    ats_type: AtsType
    slug: str
    region: str | None = None   # Lever only: 'us' | 'eu'
    job_id: str | None = None


def is_valid_slug(slug: str | None) -> bool:
    if not slug or not _SLUG_SHAPE.match(slug):
        return False
    return slug.lower() not in RESERVED_SLUGS


def extract_ats_reference(url: str | None) -> AtsReference | None:
    """Map a URL to an ATS board/job reference, or None when the host is not a known ATS."""
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host or parts.scheme.lower() not in {"http", "https"}:
        return None
    segments = [unquote(segment) for segment in parts.path.split("/") if segment]
    query = parse_qs(parts.query)

    reference: AtsReference | None = None
    if host in _GREENHOUSE_HOSTS:
        reference = _greenhouse(segments, query)
    elif host == "boards-api.greenhouse.io":
        reference = _greenhouse_api(segments)
    elif host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        reference = _lever(segments, "eu" if ".eu." in host else "us")
    elif host in {"api.lever.co", "api.eu.lever.co"}:
        reference = _lever_api(segments, "eu" if ".eu." in host else "us")
    elif host == "jobs.ashbyhq.com":
        reference = _ashby(segments)
    elif host == "apply.workable.com":
        reference = _workable_apply(segments)
    elif host.endswith(".workable.com"):
        reference = _workable_subdomain(host)
    elif host in {"jobs.smartrecruiters.com", "careers.smartrecruiters.com"}:
        reference = _smartrecruiters(segments)
    elif host.endswith(".recruitee.com"):
        reference = _recruitee(host, segments)

    if reference is not None and not is_valid_slug(reference.slug):
        return None
    return reference


def _greenhouse(segments: list[str], query: dict[str, list[str]]) -> AtsReference | None:
    if segments and segments[0].lower() == "embed":  # /embed/job_board?for=<slug>
        slug = (query.get("for") or [None])[0]
        job_id = (query.get("token") or [None])[0]
        return AtsReference(AtsType.GREENHOUSE, slug, job_id=job_id) if slug else None
    if not segments:
        return None
    job_id = segments[2] if len(segments) >= 3 and segments[1].lower() == "jobs" and segments[2].isdigit() else None
    return AtsReference(AtsType.GREENHOUSE, segments[0], job_id=job_id)


def _greenhouse_api(segments: list[str]) -> AtsReference | None:
    # /v1/boards/<slug>/jobs[/<id>]
    if len(segments) >= 3 and segments[0] == "v1" and segments[1] == "boards":
        job_id = segments[4] if len(segments) >= 5 and segments[3] == "jobs" else None
        return AtsReference(AtsType.GREENHOUSE, segments[2], job_id=job_id)
    return None


def _lever(segments: list[str], region: str) -> AtsReference | None:
    if not segments:
        return None
    job_id = segments[1] if len(segments) >= 2 and _UUID_LIKE.match(segments[1]) else None
    return AtsReference(AtsType.LEVER, segments[0], region=region, job_id=job_id)


def _lever_api(segments: list[str], region: str) -> AtsReference | None:
    # /v0/postings/<slug>[/<id>]
    if len(segments) >= 3 and segments[0] == "v0" and segments[1] == "postings":
        job_id = segments[3] if len(segments) >= 4 and _UUID_LIKE.match(segments[3]) else None
        return AtsReference(AtsType.LEVER, segments[2], region=region, job_id=job_id)
    return None


def _ashby(segments: list[str]) -> AtsReference | None:
    if not segments:
        return None
    job_id = segments[1] if len(segments) >= 2 and _UUID_LIKE.match(segments[1]) else None
    return AtsReference(AtsType.ASHBY, segments[0], job_id=job_id)


def _workable_apply(segments: list[str]) -> AtsReference | None:
    if not segments or segments[0].lower() in {"j", "api"}:  # /j/<CODE> shortlinks carry no slug
        return None
    job_id = segments[2] if len(segments) >= 3 and segments[1].lower() == "j" else None
    return AtsReference(AtsType.WORKABLE, segments[0], job_id=job_id)


def _workable_subdomain(host: str) -> AtsReference | None:
    subdomain = host[: -len(".workable.com")]
    if "." in subdomain:  # a.b.workable.com is not an account page
        return None
    return AtsReference(AtsType.WORKABLE, subdomain)


def _smartrecruiters(segments: list[str]) -> AtsReference | None:
    if len(segments) >= 3 and segments[0] == "oneclick-ui" and segments[1] == "company":
        return AtsReference(AtsType.SMARTRECRUITERS, segments[2])
    if not segments:
        return None
    job_id = None
    if len(segments) >= 2:
        match = re.match(r"^(\d{6,})", segments[1])
        job_id = match.group(1) if match else None
    return AtsReference(AtsType.SMARTRECRUITERS, segments[0], job_id=job_id)


def _recruitee(host: str, segments: list[str]) -> AtsReference | None:
    subdomain = host[: -len(".recruitee.com")]
    if "." in subdomain:
        return None
    job_id = segments[1] if len(segments) >= 2 and segments[0] == "o" else None
    return AtsReference(AtsType.RECRUITEE, subdomain, job_id=job_id)
