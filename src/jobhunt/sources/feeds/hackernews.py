"""Hacker News "Ask HN: Who is hiring?" via the Algolia HN API (no key).

* thread discovery: GET hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring
* thread content:   GET hn.algolia.com/api/v1/items/{id}  (whole comment tree in one response)

Each top-level comment is one posting. The first line conventionally reads
`Company | Role(s) | Location | Remote/Onsite | ...` (sometimes with dashes instead of pipes).
Parsing is heuristic and deliberately conservative: when a role cannot be identified the comment
is skipped rather than guessed. Comment text is untrusted user content: it is parsed as data,
never executed or followed, and only the first link is kept as the apply URL.
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlsplit

from jobhunt.errors import InvalidResponseError
from jobhunt.http import HttpClient
from jobhunt.locations import LocationParser
from jobhunt.models import FetchResult, NormalizedJob
from jobhunt.normalize import clean_text, html_to_text
from jobhunt.sources.base import FeedContext, FeedSource, as_dict, as_list, load_json
from jobhunt.timeutil import to_utc_iso

THREAD_TITLE = re.compile(r"^ask hn: who is hiring\?", re.IGNORECASE)
_LINK = re.compile(r"""<a\s[^>]*href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_BARE_URL = re.compile(r"https?://[^\s<>\"')]+")
_ROLE_WORDS = re.compile(
    r"\b(engineer|engineering|developer|programmer|swe|sde|sre|devops|scientist|researcher|analyst|architect|"
    r"designer|manager|intern|internship|cto|founder|consultant|specialist|administrator|technician)\b",
    re.IGNORECASE,
)
_NOISE_PARTS = re.compile(
    r"^(full[- ]?time|part[- ]?time|contract|intern(ship)?s?|visa|h1b|equity|salary|\$|£|€|hiring|we'?re hiring|"
    r"yc\b|series\b)", re.IGNORECASE,
)
# Only commas, semicolons and " + " separate roles: "Senior Frontend and Backend Engineer" is ONE role,
# and splitting it would silently drop its seniority.
_ROLE_SPLIT = re.compile(r"\s*(?:,|;|\s\+\s)\s*")
_MAX_TITLE_PARTS = 3
_MAX_HEADER_CHARS = 300


class HackerNewsHiringSource(FeedSource):
    name = "hackernews"
    min_interval_hours = 12.0
    stale_after_days = 45        # a thread lives for a month; posts are not individually closed
    attribution = "Posted on Hacker News, 'Ask HN: Who is hiring?' (https://news.ycombinator.com)"
    search_url = "https://hn.algolia.com/api/v1/search_by_date"
    item_url = "https://hn.algolia.com/api/v1/items"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._locations = LocationParser()

    def fetch(self, http: HttpClient, context: FeedContext) -> FetchResult:
        if not context.budget.take():
            return FetchResult(jobs=[], warnings=["hackernews: fetch cap reached"])
        search = load_json(
            http.get(self.search_url, params={"tags": "story,author_whoishiring", "hitsPerPage": 12}),
            what="hn search",
        )
        hits = as_list(as_dict(search).get("hits"))
        threads = [
            hit for hit in hits
            if isinstance(hit, dict) and THREAD_TITLE.match(str(hit.get("title", ""))) and hit.get("objectID")
        ]
        threads.sort(key=lambda hit: str(hit.get("created_at", "")), reverse=True)
        if not threads:
            raise InvalidResponseError("hn: no 'Who is hiring?' thread found in search results")

        jobs: dict[str, NormalizedJob] = {}
        malformed = 0
        warnings: list[str] = []
        for thread in threads[: int(self.option("threads", 2))]:
            if not context.budget.take():
                warnings.append("hackernews: fetch cap reached")
                break
            thread_id = str(thread["objectID"])
            tree = load_json(http.get(f"{self.item_url}/{thread_id}"), what=f"hn thread {thread_id}")
            if not isinstance(tree, dict) or not isinstance(tree.get("children"), list):
                raise InvalidResponseError(f"hn thread {thread_id}: missing 'children'")
            for comment in tree["children"]:
                job = self._to_job(comment, thread_id, context)
                if job is None:
                    malformed += 1
                else:
                    jobs.setdefault(job.source_job_id, job)
        return FetchResult(jobs=list(jobs.values()), malformed_items=malformed, warnings=warnings)

    # -- comment parsing ---------------------------------------------------------------------

    def _to_job(self, comment: Any, thread_id: str, context: FeedContext) -> NormalizedJob | None:
        if not isinstance(comment, dict) or comment.get("id") is None or not comment.get("text"):
            return None  # deleted/dead comments have no text
        raw_html = str(comment["text"])
        text = html_to_text(raw_html)
        if not text:
            return None
        header = text.split("\n", 1)[0][:_MAX_HEADER_CHARS]
        company, roles, locations = self._parse_header(header)
        if not company or not roles:
            return None

        chosen = [role for role in roles if context.keep_title(role)] or roles[:1]
        title = ", ".join(chosen[:_MAX_TITLE_PARTS])
        location_raw = "; ".join(dict.fromkeys(locations)) or None
        parsed = self._locations.parse(location_raw or header)
        apply_url = self._first_link(raw_html) or f"https://news.ycombinator.com/item?id={comment['id']}"
        thread_url = f"https://news.ycombinator.com/item?id={comment['id']}"
        return NormalizedJob(
            source=self.name, source_job_id=str(comment["id"]), title=title, company_name=company,
            apply_url=apply_url, source_url=thread_url,
            location_raw=location_raw, country=parsed.country,
            is_remote=True if re.search(r"\bremote\b", header, re.IGNORECASE) else parsed.is_remote,
            description_text=text, needs_description=False,
            posted_at=to_utc_iso(comment.get("created_at")),
            attribution_text=f"{self.attribution}, thread {thread_id}",
            raw={"author": comment.get("author"), "thread": thread_id, "header": header},
        )

    def _parse_header(self, header: str) -> tuple[str, list[str], list[str]]:
        """Return (company, role titles, location fragments) from the first line."""
        line = _BARE_URL.sub(" ", header)
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2:  # "Company - Role - Location" / "Company — Role"
            parts = [part.strip() for part in re.split(r"\s[-–—]\s", line)]
        parts = [part for part in parts if part]
        if not parts:
            return "", [], []

        parentheticals = re.findall(r"\(([^)]*)\)", parts[0])
        company = re.sub(r"\([^)]*\)", "", parts[0]).strip(" -–—:,")
        locations: list[str] = [
            value for value in parentheticals
            if re.search(r"remote|onsite|on-site|hybrid", value, re.IGNORECASE) or self._is_place(value)
        ]
        roles: list[str] = []
        for part in parts[1:]:
            cleaned = re.sub(r"\([^)]*\)", " ", part)           # drop "(TypeScript/Node.js)" style asides
            cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:")
            if not cleaned or _NOISE_PARTS.match(cleaned):
                continue
            if _ROLE_WORDS.search(cleaned):
                roles.extend(piece for piece in _ROLE_SPLIT.split(cleaned) if piece and _ROLE_WORDS.search(piece))
            elif self._is_place(part):
                locations.append(part.strip())
        return company, roles, locations

    def _is_place(self, text: str) -> bool:
        parsed = self._locations.parse(text)
        return parsed.country is not None or (parsed.is_remote is not None)

    @staticmethod
    def _first_link(raw_html: str) -> str | None:
        for match in _LINK.finditer(raw_html):
            candidate = html.unescape(match.group(1)).strip()
            host = (urlsplit(candidate).hostname or "").lower()
            if candidate.startswith(("http://", "https://")) and host and "ycombinator.com" not in host:
                return candidate
        return None
