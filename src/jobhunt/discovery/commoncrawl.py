"""Common Crawl CDX index discovery.

Politeness (per Common Crawl's FAQ / status guidance): single-threaded, spaced calls (the
HttpClient enforces `host_min_interval_seconds` for index.commoncrawl.org), a descriptive
User-Agent, 503 = back off (HttpClient retries with backoff), and every response is cached on
disk so reruns cost nothing. Page count per URL pattern is capped in config. For something
heavier than this, Common Crawl recommends their columnar index instead of the CDX API.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from jobhunt.companies import DiscoveredCompany
from jobhunt.config import CommonCrawlSettings, CrawlPattern
from jobhunt.discovery.patterns import extract_ats_reference
from jobhunt.errors import FetchError
from jobhunt.http import HttpClient

logger = logging.getLogger(__name__)


class CommonCrawlDiscovery:
    def __init__(self, settings: CommonCrawlSettings, http: HttpClient, cache_dir: Path) -> None:
        self._settings = settings
        self._http = http
        self._cache_dir = cache_dir / "commoncrawl"

    # -- crawl list ------------------------------------------------------------------------

    def latest_crawl_ids(self) -> list[str]:
        """Most recent crawl ids, newest first (collinfo.json is cached after the first call)."""
        cached = self._cache_dir / "collinfo.json"
        payload: Any
        if cached.is_file():
            payload = json.loads(cached.read_text(encoding="utf-8"))
        else:
            response = self._http.get(f"{self._settings.index_base_url}/collinfo.json")
            if response.status_code != 200:
                raise FetchError(f"collinfo.json: HTTP {response.status_code}")
            payload = response.json()
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(payload), encoding="utf-8")
        ids = [entry["id"] for entry in payload if isinstance(entry, dict) and entry.get("id")]
        return ids[: self._settings.crawls_to_query]

    # -- discovery -------------------------------------------------------------------------

    def discover(self) -> Iterator[DiscoveredCompany]:
        """Yield each distinct (ats, slug) found for every configured URL pattern."""
        seen: set[tuple[str, str]] = set()
        try:
            crawl_ids = self.latest_crawl_ids()
        except (FetchError, ValueError, KeyError) as error:
            logger.warning("common crawl: cannot list crawls (%s)", error)
            return
        for crawl_id in crawl_ids:
            for pattern in self._settings.patterns:
                for url in self._urls_for_pattern(crawl_id, pattern):
                    reference = extract_ats_reference(url if "://" in url else f"https://{url}")
                    if reference is None or reference.ats_type.value != pattern.ats:
                        continue
                    key = (reference.ats_type.value, reference.slug.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    yield DiscoveredCompany(
                        ats_type=reference.ats_type.value,
                        slug=reference.slug,
                        region=reference.region,
                        discovered_via=f"commoncrawl:{crawl_id}",
                    )

    def _urls_for_pattern(self, crawl_id: str, pattern: CrawlPattern) -> Iterator[str]:
        endpoint = f"{self._settings.index_base_url}/{crawl_id}-index"
        base_params = {"url": pattern.url, "output": "json", "fl": "url", "filter": "status:200"}
        try:
            number_of_pages = self._page_count(endpoint, base_params)
        except FetchError as error:
            logger.warning("common crawl %s %s: %s", crawl_id, pattern.url, error)
            return
        pages = min(number_of_pages, self._settings.max_pages_per_pattern)
        logger.info("common crawl %s %s: %d page(s) (cap %d)", crawl_id, pattern.url, number_of_pages,
                    self._settings.max_pages_per_pattern)
        for page in range(pages):
            try:
                body = self._cached_get(endpoint, {**base_params, "page": page}, crawl_id, pattern.url, page)
            except FetchError as error:
                logger.warning("common crawl %s %s page %d: %s (stopping this pattern)", crawl_id, pattern.url, page, error)
                return
            for line in body.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    url = json.loads(line).get("url")
                except (ValueError, AttributeError):
                    continue
                if isinstance(url, str):
                    yield url

    def _page_count(self, endpoint: str, base_params: dict[str, Any]) -> int:
        body = self._cached_get(endpoint, {**base_params, "showNumPages": "true"}, "pages", base_params["url"], 0)
        try:
            return max(0, int(json.loads(body.strip().splitlines()[0])["pages"]))
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise FetchError(f"unexpected showNumPages response: {body[:120]!r}") from error

    def _cached_get(self, endpoint: str, params: dict[str, Any], crawl_id: str, pattern_url: str, page: int) -> str:
        key = hashlib.sha1(f"{endpoint}|{sorted(params.items())}".encode("utf-8")).hexdigest()[:20]
        cache_file = self._cache_dir / crawl_id / f"{key}-{page}.txt"
        if cache_file.is_file():
            return cache_file.read_text(encoding="utf-8")
        response = self._http.get(endpoint, params=params)
        if response.status_code == 404:  # CDX answers 404 "No Captures found" for empty patterns
            body = ""
        elif response.status_code != 200:
            raise FetchError(f"HTTP {response.status_code} from index server")
        else:
            body = response.text
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(body, encoding="utf-8")
        return body
