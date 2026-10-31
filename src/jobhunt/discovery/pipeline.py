"""The `discover` command: seeds -> openroles -> Common Crawl -> snowball -> liveness probe."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path

from jobhunt.companies import DiscoveredCompany, add_discovered_companies
from jobhunt.config import Config
from jobhunt.db import transaction
from jobhunt.discovery.commoncrawl import CommonCrawlDiscovery
from jobhunt.discovery.openroles import fetch_openroles_slugs
from jobhunt.discovery.probe import ProbeSummary, probe_unverified
from jobhunt.discovery.seeds import companies_from_urls, load_seed_file, snowball_urls_from_database
from jobhunt.http import HttpClient
from jobhunt.sources.base import JobSource
from jobhunt.timeutil import Clock, utc_now

logger = logging.getLogger(__name__)

_INSERT_CHUNK = 2000


@dataclass
class DiscoverySummary:
    added: dict[str, int] = field(default_factory=dict)  # step name -> new unverified companies
    probe: ProbeSummary | None = None


def run_discovery(
    connection: sqlite3.Connection,
    config: Config,
    sources: Mapping[str, JobSource],
    http: HttpClient,
    *,
    clock: Clock = utc_now,
    seed_path: Path | None = None,
    use_seeds: bool = True,
    use_common_crawl: bool | None = None,
    use_openroles: bool | None = None,
    use_snowball: bool | None = None,
    do_probe: bool = True,
    probe_limit: int | None = None,
) -> DiscoverySummary:
    """Run the discovery steps. `None` flags fall back to config.yaml."""
    settings = config.discovery
    use_common_crawl = settings.common_crawl.enabled if use_common_crawl is None else use_common_crawl
    use_openroles = settings.openroles.enabled if use_openroles is None else use_openroles
    use_snowball = settings.snowball if use_snowball is None else use_snowball
    summary = DiscoverySummary()

    if use_seeds:
        path = seed_path or (config.resolve_path(settings.seed_file) if settings.seed_file else None)
        if path is not None and path.is_file():
            summary.added["seed"] = _store(connection, load_seed_file(path), clock)
        elif seed_path is not None:  # explicitly requested file must exist
            summary.added["seed"] = _store(connection, load_seed_file(seed_path), clock)

    if use_openroles:
        summary.added["openroles"] = _store(connection, fetch_openroles_slugs(settings.openroles, http), clock)

    if use_common_crawl:
        crawler = CommonCrawlDiscovery(settings.common_crawl, http, config.cache_directory)
        summary.added["commoncrawl"] = _store(connection, crawler.discover(), clock)

    if use_snowball:
        urls = snowball_urls_from_database(connection)
        summary.added["snowball"] = _store(connection, companies_from_urls(urls, "snowball"), clock)

    if do_probe:
        limit = probe_limit if probe_limit is not None else settings.default_probe_limit
        summary.probe = probe_unverified(connection, config, sources, http, clock=clock, limit=limit)
    return summary


def _store(connection: sqlite3.Connection, companies: Iterable[DiscoveredCompany], clock: Clock) -> int:
    """Insert in chunks so a multi-million-URL crawl never sits in memory or one giant statement."""
    iterator = iter(companies)
    added = 0
    while chunk := list(islice(iterator, _INSERT_CHUNK)):
        with transaction(connection):
            added += add_discovered_companies(connection, chunk, clock())
    return added
