"""Extract job cards (title, company, location, link) from job-alert email HTML.

Pure parsing: BeautifulSoup over the message body, string handling over URLs, no network, no
LLM. The email text is untrusted data: only fields are extracted, anything resembling an
instruction is ignored (it is simply text that never matches a field).

How a card is found
-------------------
1. Every `<a href>` is classified as a job link (platform URL shape, or a link already hosted
   on a supported ATS, or a verbatim link on a platform host with a title-like anchor text).
2. For each job link the *card* is the highest ancestor that still contains only that one
   job link and a modest amount of text.
3. Card lines minus the title and UI noise ("Easy Apply", "View job"...) yield company and
   location: `Company · Location` on one line, or separate lines.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
from bs4.element import Tag

from jobhunt.alerts.urls import PLATFORM_HOSTS, JobLink, host_matches, recognize, stable_id
from jobhunt.dedupe import canonicalize_url
from jobhunt.discovery.patterns import AtsReference, extract_ats_reference
from jobhunt.locations import LocationParser

_MAX_CARD_TEXT = 700
_MAX_TITLE = 160
_MAX_COMPANY = 90

_GENERIC_LINK_TEXT = re.compile(
    r"^(view|see|apply|easy apply|apply now|view job|view details|view all|see all|view more|see more|"
    r"unsubscribe|manage|settings|preferences|edit|help|privacy|terms|download|open|click here|read more|"
    r"learn more|get the app|jobs|search jobs|job alerts?|update|your alert|turn off|here)\b.*$",
    re.IGNORECASE,
)
_NOISE_LINE = re.compile(
    r"^(easy apply|apply( now)?|view( job| details)?|see (job|details)|save|saved|new|promoted|"
    r"be an early applicant|actively (recruiting|hiring)|\d+\+? (applicants?|connections?|alumni)|"
    r"posted .*|.* ago|today|just now|verified|hiring|new jobs?|recommended|top match|\d+% match|"
    r"quick apply|apply on company site|[•·|\-–—]+)$",
    re.IGNORECASE,
)
_META_LINE = re.compile(
    r"(₹|\$|£|€|\blpa\b|\bctc\b|per (month|annum|year|hour)|/ ?(month|year|yr|hr)|\bstipend\b|\bsalary\b|"
    r"\b\d+\s*[-–to]+\s*\d+\s*(yrs?|years?)\b|\b\d+\+?\s*(yrs?|years?)\b|\bfull[- ]?time\b|\bpart[- ]?time\b|"
    r"\bexperience\b|\bopenings?\b|\bapplicants?\b|\bdays? left\b)",
    re.IGNORECASE,
)
_SEPARATOR = re.compile(r"\s[·•|]\s|\s-\s|\s–\s|\s—\s")
_NON_JOB_PATH = re.compile(
    r"/(unsubscribe|settings|preferences|help|privacy|terms|alerts?|account|login|signup|jobs/search|"
    r"jobs/collections|jobs-in|salaries|company-reviews|dashboard|student|profile|mnjuser|competitions|"
    r"hackathons?|quizzes|workshops|webinars|scholarships|events|blog|about|contact|careers)\b",
    re.IGNORECASE,
)
_ID_TOKEN = re.compile(r"\d{5,}|[0-9a-f]{12,}", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedAlertJob:
    platform: str
    title: str
    company: str
    location: str | None
    url: str                      # canonical when recognized, otherwise the verbatim email link
    job_id: str | None
    recognized: bool
    ats: AtsReference | None = None


def parse_alert_html(platform: str, html: str, locations: LocationParser) -> list[ParsedAlertJob]:
    """All job cards found in one alert email body, de-duplicated by canonical link."""
    if not html or not html.strip():
        return []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "noscript"]):
        tag.decompose()

    candidates: list[tuple[Tag, JobLink, AtsReference | None, str]] = []
    for anchor in soup.find_all("a", href=True):
        text = _clean(anchor.get_text(" "))
        classified = _classify(platform, str(anchor["href"]), text)
        if classified is not None:
            link, ats = classified
            candidates.append((anchor, link, ats, text))
    if not candidates:
        return []

    url_by_anchor = {id(anchor): link.url for anchor, link, _, _ in candidates}
    jobs: dict[str, ParsedAlertJob] = {}
    for anchor, link, ats, text in candidates:
        card_lines = _card_lines(anchor, link.url, url_by_anchor)
        title, remaining = _pick_title(text, card_lines, locations, min_words=1 if link.recognized else 2)
        if not title:
            continue
        company, location = _company_and_location(remaining, locations)
        job = ParsedAlertJob(
            platform=platform, title=title, company=company or "", location=location, url=link.url,
            job_id=link.job_id or stable_id(link.url), recognized=link.recognized, ats=ats,
        )
        existing = jobs.get(link.url)
        if existing is None or (not existing.company and job.company):
            jobs[link.url] = job
    return list(jobs.values())


def parse_alert_text(platform: str, text: str, locations: LocationParser) -> list[ParsedAlertJob]:
    """Plain-text alert bodies: a recognized URL line preceded by title/company lines."""
    lines = [line.strip() for line in text.splitlines()]
    jobs: dict[str, ParsedAlertJob] = {}
    for index, line in enumerate(lines):
        match = re.search(r"https?://\S+", line)
        if not match:
            continue
        url = match.group(0).rstrip(").,>")
        classified = _classify(platform, url, "")
        if classified is None:
            continue
        link, ats = classified
        block: list[str] = []   # the contiguous non-empty lines directly above the link
        for previous in reversed(lines[max(0, index - 5):index]):
            if not previous or re.search(r"https?://", previous):
                break
            block.append(previous)
        context = [candidate for candidate in reversed(block) if not _NOISE_LINE.match(candidate)]
        title, remaining = _pick_title("", context, locations)
        if not title:
            continue
        company, location = _company_and_location(remaining, locations)
        jobs.setdefault(link.url, ParsedAlertJob(
            platform, title, company or "", location, link.url, link.job_id or stable_id(link.url), link.recognized, ats))
    return list(jobs.values())


# -- link classification ------------------------------------------------------------------------

def _classify(platform: str, href: str, anchor_text: str) -> tuple[JobLink, AtsReference | None] | None:
    href = href.strip()
    ats = extract_ats_reference(href)
    if ats is not None and ats.job_id:
        canonical = canonicalize_url(href)
        if canonical:
            return JobLink(canonical, ats.job_id), ats
    link = recognize(platform, href)
    if link is not None:
        return link, None
    # Unknown shape on a platform host: keep the link verbatim when it looks like a job title.
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(href)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if (
        parts.scheme in {"http", "https"} and host_matches(host, PLATFORM_HOSTS.get(platform, ()))
        and not _NON_JOB_PATH.search(parts.path) and _has_id_token(parts.path, parts.query)
        and _looks_like_title(anchor_text)
    ):
        return JobLink(href.split("#", 1)[0], None, recognized=False), None
    return None


def _has_id_token(path: str, query: str) -> bool:
    """A job page URL carries an identifier; navigation pages ("/student/dashboard") do not."""
    return bool(_ID_TOKEN.search(path) or re.search(r"(?:^|&)(?:jk|jid|id|jobid|job_id)=", query, re.IGNORECASE))


def _looks_like_title(text: str, min_words: int = 2) -> bool:
    if not text or len(text) > _MAX_TITLE or _GENERIC_LINK_TEXT.match(text):
        return False
    words = text.split()
    return min_words <= len(words) <= 18 and bool(re.search(r"[A-Za-z]", text))


# -- card extraction ----------------------------------------------------------------------------

def _card_lines(anchor: Tag, own_url: str, url_by_anchor: dict[int, str]) -> list[str]:
    container: Tag = anchor
    while container.parent is not None and isinstance(container.parent, Tag):
        parent = container.parent
        if parent.name in {"body", "html", "[document]"}:
            break
        other_urls = {
            url_by_anchor[id(other)]
            for other in parent.find_all("a", href=True)
            if id(other) in url_by_anchor and url_by_anchor[id(other)] != own_url
        }
        if other_urls or len(parent.get_text(" ", strip=True)) > _MAX_CARD_TEXT:
            break
        container = parent
    lines: list[str] = []
    for raw in container.get_text("\n").split("\n"):
        line = _clean(raw)
        if line:
            lines.append(line)
    return lines


def _pick_title(
    anchor_text: str, lines: list[str], locations: LocationParser, *, min_words: int = 1
) -> tuple[str, list[str]]:
    title = anchor_text if _looks_like_title(anchor_text, min_words) else ""
    remaining = [line for line in lines if line.lower() != title.lower() and not _NOISE_LINE.match(line)]
    if not title:
        for candidate in remaining:
            if _looks_like_title(candidate, min_words) and not _looks_like_location(candidate, locations) \
                    and not _META_LINE.search(candidate):
                title = candidate
                break
        remaining = [line for line in remaining if line != title]
    return title[:_MAX_TITLE], remaining


def _looks_like_location(text: str, locations: LocationParser) -> bool:
    return locations.place_strength(text) >= 2


def _company_and_location(lines: list[str], locations: LocationParser) -> tuple[str | None, str | None]:
    company: str | None = None
    location: str | None = None
    for line in lines[:8]:
        if _META_LINE.search(line) and not _looks_like_location(line, locations):
            continue
        parts = [part.strip() for part in _SEPARATOR.split(line) if part.strip()]
        if len(parts) >= 2:  # "Company · Location" (LinkedIn style) or "City · Remote"
            remainder = parts
            if company is None and not _looks_like_location(parts[0], locations):
                company = parts[0][:_MAX_COMPANY]
                remainder = parts[1:]
            places = [part for part in remainder if _looks_like_location(part, locations)]
            if places and location is None:
                location = " · ".join(places)
            if company and location:
                break
            continue
        if _looks_like_location(line, locations):
            if location is None:
                location = line
        elif company is None and len(line) <= _MAX_COMPANY:
            company = line
        if company and location:
            break
    return company, location


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
