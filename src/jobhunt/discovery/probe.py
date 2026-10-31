"""Liveness probing: promote `unverified` companies to `active` (or start counting failures).

A company is promoted when its API answers with valid JSON that proves the board exists
(`JobSource.proves_board_exists`): at least one job, or a valid empty board for sources where
an empty board is distinguishable from an unknown slug. Probing is done in polite batches.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Collection, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from jobhunt import companies as company_repo
from jobhunt.config import Config
from jobhunt.db import transaction
from jobhunt.errors import FetchError, InvalidResponseError, NotFoundError
from jobhunt.http import HttpClient
from jobhunt.models import CompanyRef, FetchResult
from jobhunt.sources.base import FetchContext, JobSource
from jobhunt.timeutil import Clock, utc_now

logger = logging.getLogger(__name__)

_NO_DETAIL = FetchContext(wants_detail=lambda job: False, detail_cap=0)


@dataclass
class ProbeSummary:
    probed: int = 0
    promoted: int = 0
    failed: int = 0
    deactivated: int = 0
    transient: int = 0
    skipped_no_adapter: int = 0
    errors: list[str] = field(default_factory=list)


def probe_unverified(
    connection: sqlite3.Connection,
    config: Config,
    sources: Mapping[str, JobSource],
    http: HttpClient,
    *,
    clock: Clock = utc_now,
    limit: int | None = None,
    company_ids: Collection[int] | None = None,
) -> ProbeSummary:
    """Probe unverified companies (those with an enabled adapter), `probe_batch_size` at a time."""
    summary = ProbeSummary()
    settings = config.discovery
    now = clock()
    candidates = company_repo.companies_to_probe(
        connection, now, settings.probe_min_interval_hours, ats_types=list(sources), limit=limit,
        only_ids=company_ids,
    )
    logger.info("probing %d unverified compan%s", len(candidates), "y" if len(candidates) == 1 else "ies")

    for start in range(0, len(candidates), settings.probe_batch_size):
        batch = candidates[start : start + settings.probe_batch_size]
        workers = max(1, min(config.http.max_workers, len(batch)))

        def probe_one(company: CompanyRef) -> tuple[CompanyRef, FetchResult | None, Exception | None]:
            adapter = sources[company.ats_type]
            try:
                return company, adapter.fetch_company(company, http, _NO_DETAIL), None
            except FetchError as error:
                return company, None, error
            except Exception as error:  # adapter bug: never abort the batch
                logger.exception("probe %s/%s crashed", company.ats_type, company.slug)
                return company, None, error

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe") as pool:
            results = list(pool.map(probe_one, batch))

        for company, result, error in results:
            summary.probed += 1
            moment = clock()
            adapter = sources[company.ats_type]
            with transaction(connection):
                if result is not None and adapter.proves_board_exists(result):
                    company_repo.record_success(
                        connection, company.id, moment, name=result.company_name, region=result.region
                    )
                    summary.promoted += 1
                elif result is not None:  # valid but empty and not conclusive (e.g. SmartRecruiters)
                    status = company_repo.record_failure(
                        connection, company.id, moment, "empty board does not prove the company exists",
                        config.lifecycle,
                    )
                    summary.failed += 1
                    summary.deactivated += status == "inactive"
                elif isinstance(error, (NotFoundError, InvalidResponseError)):
                    status = company_repo.record_failure(connection, company.id, moment, str(error), config.lifecycle)
                    summary.failed += 1
                    summary.deactivated += status == "inactive"
                else:
                    company_repo.record_transient_problem(connection, company.id, moment, str(error))
                    summary.transient += 1
                    summary.errors.append(f"{company.ats_type}/{company.slug}: {error}")
    return summary
