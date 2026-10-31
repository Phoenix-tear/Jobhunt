"""Hand-maintained seed file and the snowball from URLs we already hold."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path

import yaml

from jobhunt.companies import DiscoveredCompany
from jobhunt.discovery.patterns import extract_ats_reference, is_valid_slug
from jobhunt.errors import ConfigError
from jobhunt.models import AtsType

logger = logging.getLogger(__name__)

_KNOWN_ATS = {member.value for member in AtsType if member is not AtsType.UNKNOWN}


def load_seed_file(path: Path) -> list[DiscoveredCompany]:
    """Read `seeds/companies.yaml`:

        companies:
          - {ats: greenhouse, slug: stripe, name: Stripe}
          - {ats: lever, slug: spotify, region: eu}
    """
    if not path.is_file():
        raise ConfigError(f"seed file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"invalid YAML in {path}: {error}") from error
    entries = data.get("companies") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: expected a top-level 'companies:' list")

    companies: list[DiscoveredCompany] = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            logger.warning("%s entry %d is not a mapping, skipped", path.name, position)
            continue
        ats, slug = str(entry.get("ats", "")).strip().lower(), str(entry.get("slug", "")).strip()
        if ats not in _KNOWN_ATS or not is_valid_slug(slug):
            logger.warning("%s entry %d (ats=%r slug=%r) is invalid, skipped", path.name, position, ats, slug)
            continue
        region = entry.get("region")
        companies.append(
            DiscoveredCompany(
                ats_type=ats, slug=slug, name=entry.get("name"),
                region=str(region).lower() if region in ("us", "eu") else None,
                discovered_via="seed",
            )
        )
    return companies


def companies_from_urls(urls: Iterable[str | None], discovered_via: str) -> Iterator[DiscoveredCompany]:
    """Snowball: every URL whose host is a supported ATS contributes its board slug."""
    seen: set[tuple[str, str]] = set()
    for url in urls:
        reference = extract_ats_reference(url)
        if reference is None:
            continue
        key = (reference.ats_type.value, reference.slug.lower())
        if key in seen:
            continue
        seen.add(key)
        yield DiscoveredCompany(
            ats_type=reference.ats_type.value, slug=reference.slug, region=reference.region,
            discovered_via=discovered_via,
        )


def snowball_urls_from_database(connection: sqlite3.Connection) -> Iterator[str]:
    """Every apply/source URL stored so far (any source: ATS, aggregator, HN, email)."""
    for row in connection.execute(
        "SELECT canonical_apply_url AS url FROM jobs WHERE canonical_apply_url IS NOT NULL "
        "UNION SELECT source_url FROM job_sightings WHERE source_url IS NOT NULL"
    ):
        yield row["url"]
