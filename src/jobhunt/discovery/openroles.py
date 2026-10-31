"""Optional slug lists from github.com/datascry/openroles.

Licensing decision (checked 2026-10-12): the repository's code is MIT and its dataset is
CC BY-SA 4.0 (LICENSE-DATA). Share-alike would attach to anything redistributed, so these
lists are NOT vendored into this repository. They are fetched on demand into the local,
gitignored SQLite file only (private use, nothing republished), and `discovered_via` records
the origin. Attribution: "Tenant lists courtesy of datascry/openroles, CC BY-SA 4.0".
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from jobhunt.companies import DiscoveredCompany
from jobhunt.config import OpenRolesSettings
from jobhunt.discovery.patterns import is_valid_slug
from jobhunt.errors import FetchError
from jobhunt.http import HttpClient
from jobhunt.models import AtsType

logger = logging.getLogger(__name__)

ATTRIBUTION = "Tenant lists courtesy of datascry/openroles (https://github.com/datascry/openroles), CC BY-SA 4.0"
_SUPPORTED = [member.value for member in AtsType if member is not AtsType.UNKNOWN]


def fetch_openroles_slugs(settings: OpenRolesSettings, http: HttpClient) -> Iterator[DiscoveredCompany]:
    logger.info(ATTRIBUTION)
    for ats in _SUPPORTED:
        url = f"{settings.base_url}/{ats}.json"
        try:
            response = http.get(url)
        except FetchError as error:
            logger.warning("openroles %s: %s", ats, error)
            continue
        if response.status_code != 200:
            logger.warning("openroles %s: HTTP %d, skipped", ats, response.status_code)
            continue
        try:
            entries = response.json()
        except ValueError:
            logger.warning("openroles %s: not JSON, skipped", ats)
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            slug = entry.get("slug")
            status = entry.get("status")
            if isinstance(slug, str) and is_valid_slug(slug) and (not settings.only_status or status == settings.only_status):
                yield DiscoveredCompany(ats_type=ats, slug=slug, discovered_via="openroles")
