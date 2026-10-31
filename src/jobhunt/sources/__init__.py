"""Source registry: config.yaml `sources:` block -> instantiated adapters."""

from __future__ import annotations

from collections.abc import Mapping

from jobhunt.config import Config
from jobhunt.sources.ashby import AshbySource
from jobhunt.sources.base import FeedContext, FeedSource, FetchBudget, FetchContext, JobSource
from jobhunt.sources.feeds import FEED_SOURCE_CLASSES
from jobhunt.sources.greenhouse import GreenhouseSource
from jobhunt.sources.lever import LeverSource
from jobhunt.sources.recruitee import RecruiteeSource
from jobhunt.sources.smartrecruiters import SmartRecruitersSource
from jobhunt.sources.workable import WorkableSource

# Order = the order the runner polls them (matches the brief's priority).
ATS_SOURCE_CLASSES: tuple[type[JobSource], ...] = (
    GreenhouseSource,
    LeverSource,
    AshbySource,
    WorkableSource,
    SmartRecruitersSource,
    RecruiteeSource,
)

# Sightings from these sources are authoritative for lifecycle: a feed never closes their jobs.
ATS_SOURCE_NAMES = frozenset(source_class.name for source_class in ATS_SOURCE_CLASSES)


def build_ats_sources(config: Config) -> dict[str, JobSource]:
    """Instantiate every ATS adapter that is enabled in config.yaml, keyed by source name."""
    sources: dict[str, JobSource] = {}
    for source_class in ATS_SOURCE_CLASSES:
        if config.source_enabled(source_class.name):
            sources[source_class.name] = source_class(config.sources[source_class.name].options)
    return sources


def _detected_enterprise(config: Config, platform: str) -> list[dict]:
    import sqlite3

    from jobhunt.enterprise.registry import detected_companies

    path = config.database_file
    if not path.is_file():
        return []
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return detected_companies(connection, platform)
    finally:
        connection.close()


def build_feed_sources(config: Config, secrets: Mapping[str, str] | None = None) -> dict[str, FeedSource]:
    """Instantiate every enabled aggregator / feed / curated-list adapter."""
    feeds: dict[str, FeedSource] = {}
    for feed_class in FEED_SOURCE_CLASSES:
        if config.source_enabled(feed_class.name):
            options = dict(config.sources[feed_class.name].options)
            if feed_class.name in {"workday", "eightfold", "phenom", "oracle"}:   # from `detect-ats`
                options["companies"] = _detected_enterprise(config, feed_class.name)
            feeds[feed_class.name] = feed_class(options, secrets)
    return feeds


def build_all_sources(config: Config, secrets: Mapping[str, str] | None = None) -> dict[str, JobSource | FeedSource]:
    return {**build_ats_sources(config), **build_feed_sources(config, secrets)}


__all__ = [
    "ATS_SOURCE_CLASSES", "ATS_SOURCE_NAMES", "FEED_SOURCE_CLASSES", "FeedContext", "FeedSource", "FetchBudget",
    "FetchContext", "JobSource", "build_all_sources", "build_ats_sources", "build_feed_sources",
]
