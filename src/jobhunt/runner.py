"""Ingestion runner: fetch boards in parallel, apply results one company at a time.

Concurrency model: worker threads only do network I/O (the HttpClient throttles per host);
every database write happens on the calling thread, one transaction per company, so SQLite is
never touched from two threads and one broken board can never poison another one.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from jobhunt import companies as company_repo
from jobhunt.classify import Classifier, TitlePrefilter
from jobhunt.config import Config
from jobhunt.db import transaction
from jobhunt.dedupe import UpsertStats, upsert_jobs
from jobhunt.errors import AuthRequiredError, FetchError, InvalidResponseError, NotFoundError
from jobhunt.http import HttpClient
from jobhunt.lifecycle import (
    close_by_source_ids, close_stale_feed_jobs, count_open_jobs_in_scope, is_suspicious_drop, register_misses,
)
from jobhunt.locations import LocationParser
from jobhunt.models import CompanyRef, FetchResult, NormalizedJob
from jobhunt.normalize import enrich_job
from jobhunt.sources import ATS_SOURCE_NAMES
from jobhunt.sources.base import FeedContext, FeedSource, FetchBudget, FetchContext, JobSource
from jobhunt.state import fetches_today, get_cursor, record_source_run, set_cursor
from jobhunt.timeutil import Clock, parse_iso, to_iso, utc_now

logger = logging.getLogger(__name__)

MAX_RECORDED_ERRORS = 200


@dataclass
class RunSummary:
    """Counters for one source in one run (mirrors an `ingestion_runs` row)."""

    source: str
    companies_checked: int = 0
    companies_failed: int = 0
    jobs_seen: int = 0
    jobs_new: int = 0
    jobs_merged: int = 0
    jobs_updated: int = 0
    jobs_reopened: int = 0
    jobs_closed: int = 0
    jobs_skipped_by_prefilter: int = 0
    blocked: bool = False
    skipped_reason: str | None = None   # feeds: why nothing was fetched (interval, cap, missing key)
    errors: list[dict[str, str]] = field(default_factory=list)

    def add_error(self, company: CompanyRef, kind: str, message: str) -> None:
        self.add_error_for(f"{company.ats_type}/{company.slug}", kind, message)

    def add_error_for(self, subject: str, kind: str, message: str) -> None:
        if len(self.errors) < MAX_RECORDED_ERRORS:
            self.errors.append({"company": subject, "kind": kind, "message": message[:300]})


@dataclass
class _Outcome:
    company: CompanyRef
    result: FetchResult | None = None
    error: Exception | None = None
    skipped: bool = False


class IngestionRunner:
    def __init__(
        self,
        connection: sqlite3.Connection,
        config: Config,
        sources: Mapping[str, JobSource | FeedSource],
        http: HttpClient,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._connection = connection
        self._config = config
        self._sources = dict(sources)
        self._http = http
        self._clock = clock
        self._prefilter = TitlePrefilter.from_config(config)
        self._classifier = Classifier.from_config(config)
        self._locations = LocationParser(config.classification.extra_city_countries)

    # -- public ----------------------------------------------------------------------------

    def run(
        self,
        source_names: Sequence[str] | None = None,
        company_limit: int | None = None,
        *,
        force: bool = False,
        company_ids: Collection[int] | None = None,
    ) -> list[RunSummary]:
        """Ingest every requested source (default: all registered), one `ingestion_runs` row each.

        `force` ignores a feed's minimum interval (never its daily fetch cap).
        """
        names = list(source_names) if source_names else list(self._sources)
        unknown = [name for name in names if name not in self._sources]
        if unknown:
            raise KeyError(f"source(s) not enabled or unknown: {', '.join(unknown)}")
        summaries: list[RunSummary] = []
        for name in names:
            source = self._sources[name]
            if isinstance(source, FeedSource):
                summaries.append(self._run_feed_source(source, force))
            else:
                summaries.append(self._run_source(source, company_limit, company_ids))
        return summaries

    # -- one source ------------------------------------------------------------------------

    def _run_source(
        self, source: JobSource, company_limit: int | None, company_ids: Collection[int] | None = None
    ) -> RunSummary:
        started_at = self._clock()
        summary = RunSummary(source=source.name)
        run_id = self._open_run_row(source.name, started_at)

        companies = company_repo.companies_due_for_ingest(
            self._connection, source.ats_type.value, started_at, self._config.lifecycle, company_limit,
            only_ids=company_ids,
        )
        logger.info("%s: %d compan%s due", source.name, len(companies), "y" if len(companies) == 1 else "ies")
        requests_before = self._http.request_count

        known_with_description = self._known_with_description(source.name)
        context = FetchContext(
            wants_detail=lambda job: (
                job.source_job_id not in known_with_description and self._prefilter.keeps(job.title)
            ),
            detail_cap=self._config.lifecycle.detail_fetch_cap_per_company,
        )
        blocked = threading.Event()

        def fetch(company: CompanyRef) -> _Outcome:
            if blocked.is_set():
                return _Outcome(company, skipped=True)
            try:
                return _Outcome(company, result=source.fetch_company(company, self._http, context))
            except AuthRequiredError as error:
                blocked.set()
                return _Outcome(company, error=error)
            except FetchError as error:
                return _Outcome(company, error=error)
            except Exception as error:  # adapter bug: isolate it, never abort the run
                logger.exception("%s/%s: unexpected adapter error", source.name, company.slug)
                return _Outcome(company, error=error)

        if companies:
            workers = max(1, min(self._config.http.max_workers, len(companies)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"ingest-{source.name}") as pool:
                futures = [pool.submit(fetch, company) for company in companies]
                for future in as_completed(futures):
                    self._apply_outcome(source, future.result(), summary)

        summary.blocked = blocked.is_set()
        finished_at = self._clock()
        fetches = self._http.request_count - requests_before
        with transaction(self._connection):
            self._close_run_row(run_id, finished_at, summary)
            record_source_run(
                self._connection, source.name, finished_at,
                success=summary.companies_checked > summary.companies_failed or not companies,
                fetches=fetches,
            )
        logger.info(
            "%s: checked=%d failed=%d seen=%d new=%d closed=%d prefiltered=%d",
            source.name, summary.companies_checked, summary.companies_failed, summary.jobs_seen,
            summary.jobs_new, summary.jobs_closed, summary.jobs_skipped_by_prefilter,
        )
        return summary

    # -- one feed ----------------------------------------------------------------------------

    def _run_feed_source(self, source: FeedSource, force: bool) -> RunSummary:
        started_at = self._clock()
        summary = RunSummary(source=source.name)
        run_id = self._open_run_row(source.name, started_at)

        reason = self._feed_skip_reason(source, started_at, force)
        cap = source.fetch_cap_per_day
        available = None if cap is None else max(0, cap - fetches_today(self._connection, source.name, started_at))
        if reason is None and available is not None and available <= 0:
            reason = f"daily fetch cap of {cap} already used"
        if reason is not None:
            summary.skipped_reason = reason
            summary.add_error_for(source.name, "skipped", reason)
            logger.info("%s: skipped (%s)", source.name, reason)
            with transaction(self._connection):
                self._close_run_row(run_id, self._clock(), summary)
            return summary

        context = FeedContext(
            now=started_at, budget=FetchBudget(available),
            cursor=get_cursor(self._connection, source.name), keep_title=self._prefilter.keeps,
        )
        result: FetchResult | None
        try:
            result = source.fetch(self._http, context)
        except FetchError as error:
            summary.blocked = isinstance(error, AuthRequiredError)
            summary.add_error_for(source.name, getattr(error, "kind", "fetch_error"), str(error))
            result = None
        except Exception as error:  # adapter bug: isolate it
            logger.exception("%s: unexpected feed adapter error", source.name)
            summary.add_error_for(source.name, "unexpected_error", f"{type(error).__name__}: {error}")
            result = None

        finished_at = self._clock()
        with transaction(self._connection):
            if result is not None:
                self._apply_feed_result(source, result, summary, finished_at)
                if result.cursor is not None:
                    set_cursor(self._connection, source.name, result.cursor)
            self._close_run_row(run_id, finished_at, summary)
            record_source_run(
                self._connection, source.name, finished_at, success=result is not None, fetches=context.budget.used
            )
        logger.info(
            "%s: seen=%d new=%d merged=%d closed=%d prefiltered=%d%s",
            source.name, summary.jobs_seen, summary.jobs_new, summary.jobs_merged, summary.jobs_closed,
            summary.jobs_skipped_by_prefilter, "" if result is not None else " (FAILED)",
        )
        return summary

    def _feed_skip_reason(self, source: FeedSource, now: datetime, force: bool) -> str | None:
        missing = source.missing_secrets()
        if missing:
            return f"missing secret(s) {', '.join(missing)}; set them in .env (see .env.example)"
        if force:
            return None
        row = self._connection.execute(
            "SELECT last_run_at FROM source_state WHERE source = ?", (source.name,)
        ).fetchone()
        if row and row["last_run_at"]:
            elapsed = now - parse_iso(row["last_run_at"])
            if elapsed < timedelta(hours=source.interval_hours):
                hours = elapsed.total_seconds() / 3600
                return f"last ran {hours:.1f}h ago (minimum interval {source.interval_hours:g}h)"
        return None

    def _apply_feed_result(self, source: FeedSource, result: FetchResult, summary: RunSummary, now: datetime) -> None:
        seen_at = to_iso(now)
        for warning in result.warnings:
            summary.add_error_for(source.name, "warning", warning)

        if result.not_modified:
            # Conditional GET said "unchanged": everything we hold from it is still listed.
            self._connection.execute(
                """UPDATE jobs SET last_seen_at = ?
                   WHERE status = 'open' AND id IN (SELECT job_id FROM job_sightings WHERE source = ?)""",
                (seen_at, source.name),
            )
            self._connection.execute(
                """UPDATE job_sightings SET last_seen_at = ?
                   WHERE source = ? AND job_id IN (SELECT id FROM jobs WHERE status = 'open')""",
                (seen_at, source.name),
            )
            return

        kept: list[NormalizedJob] = []
        skipped_ids: set[str] = set()
        for job in result.jobs:
            if not self._prefilter.keeps(job.title):
                skipped_ids.add(job.source_job_id)
                continue
            enrich_job(job, self._locations, self._classifier)
            kept.append(job)

        stats = upsert_jobs(self._connection, kept, company_id=None, seen_at=seen_at)
        to_close = skipped_ids | result.closed_source_job_ids
        closed = (
            close_by_source_ids(self._connection, source.name, to_close, seen_at, ATS_SOURCE_NAMES) if to_close else 0
        )
        stale_cutoff = to_iso(now - timedelta(days=source.stale_days))
        closed += close_stale_feed_jobs(self._connection, source.name, stale_cutoff, seen_at, ATS_SOURCE_NAMES)
        summary.jobs_seen += len(result.jobs)
        summary.jobs_new += stats.created
        summary.jobs_merged += stats.merged
        summary.jobs_updated += stats.updated
        summary.jobs_reopened += stats.reopened
        summary.jobs_closed += closed
        summary.jobs_skipped_by_prefilter += len(skipped_ids)

    # -- applying results ------------------------------------------------------------------

    def _apply_outcome(self, source: JobSource, outcome: _Outcome, summary: RunSummary) -> None:
        if outcome.skipped:
            return
        summary.companies_checked += 1
        now = self._clock()
        company = outcome.company

        if outcome.result is not None:
            self._apply_success(source, company, outcome.result, summary, now)
            return

        error = outcome.error
        assert error is not None
        summary.companies_failed += 1
        kind = getattr(error, "kind", "unexpected_error")
        summary.add_error(company, kind, str(error))
        with transaction(self._connection):
            if isinstance(error, (NotFoundError, InvalidResponseError)):
                company_repo.record_failure(self._connection, company.id, now, str(error), self._config.lifecycle)
            else:  # transient, auth-required, or an adapter bug: not evidence the slug is dead
                company_repo.record_transient_problem(self._connection, company.id, now, f"{kind}: {error}")

    def _apply_success(
        self, source: JobSource, company: CompanyRef, result: FetchResult, summary: RunSummary, now: datetime
    ) -> None:
        kept: list[NormalizedJob] = []
        skipped_ids: set[str] = set()
        for job in result.jobs:
            if not self._prefilter.keeps(job.title):
                skipped_ids.add(job.source_job_id)
                continue
            job.company_id = company.id
            job.company_name = job.company_name or result.company_name or company.name
            enrich_job(job, self._locations, self._classifier)
            kept.append(job)

        seen_at = to_iso(now)
        present_ids = result.seen_source_job_ids
        settings = self._config.lifecycle

        with transaction(self._connection):
            previous_open = count_open_jobs_in_scope(self._connection, source.name, company.id)
            suspicious = is_suspicious_drop(previous_open, len(present_ids), settings)
            empty_runs = company_repo.get_company(self._connection, company.id)["consecutive_empty_runs"]
            empty_runs = empty_runs + 1 if suspicious else 0
            trust_for_closing = not suspicious or empty_runs >= settings.suspicious_empty_runs_before_accept

            stats: UpsertStats = upsert_jobs(self._connection, kept, company_id=company.id, seen_at=seen_at)
            # Jobs we hold that the current prefilter rejects are closed (they reopen if the
            # config changes back), so editing keyword lists applies retroactively.
            closed = (
                close_by_source_ids(self._connection, source.name, skipped_ids, seen_at, ATS_SOURCE_NAMES)
                if skipped_ids else 0
            )
            if source.authoritative and trust_for_closing:
                closed += register_misses(self._connection, source.name, company.id, present_ids, seen_at, settings)
            elif suspicious:
                message = (
                    f"suspicious board size ({len(present_ids)} now vs {previous_open} open before); "
                    f"not closing jobs (run {empty_runs}/{settings.suspicious_empty_runs_before_accept})"
                )
                logger.warning("%s/%s: %s", source.name, company.slug, message)
                summary.add_error(company, "suspicious_drop", message)

            for warning in result.warnings:
                summary.add_error(company, "warning", warning)
            company_repo.record_success(
                self._connection, company.id, now,
                name=result.company_name, region=result.region, consecutive_empty_runs=empty_runs,
            )

        summary.jobs_seen += len(result.jobs)
        summary.jobs_new += stats.created
        summary.jobs_merged += stats.merged
        summary.jobs_updated += stats.updated
        summary.jobs_reopened += stats.reopened
        summary.jobs_closed += closed
        summary.jobs_skipped_by_prefilter += len(skipped_ids)

    # -- bookkeeping -----------------------------------------------------------------------

    def _known_with_description(self, source_name: str) -> frozenset[str]:
        rows = self._connection.execute(
            """SELECT s.source_job_id FROM job_sightings s JOIN jobs j ON j.id = s.job_id
               WHERE s.source = ? AND j.needs_description = 0""",
            (source_name,),
        )
        return frozenset(row[0] for row in rows)

    def _open_run_row(self, source_name: str, started_at: datetime) -> int:
        with transaction(self._connection):
            cursor = self._connection.execute(
                "INSERT INTO ingestion_runs (source, started_at) VALUES (?, ?)", (source_name, to_iso(started_at))
            )
        return int(cursor.lastrowid)

    def _close_run_row(self, run_id: int, finished_at: datetime, summary: RunSummary) -> None:
        errors: Any = summary.errors or None
        self._connection.execute(
            """UPDATE ingestion_runs SET finished_at = ?, companies_checked = ?, jobs_seen = ?,
                      jobs_new = ?, jobs_closed = ?, jobs_skipped_by_prefilter = ?, errors_json = ?
               WHERE id = ?""",
            (
                to_iso(finished_at), summary.companies_checked, summary.jobs_seen, summary.jobs_new,
                summary.jobs_closed, summary.jobs_skipped_by_prefilter,
                json.dumps(errors, ensure_ascii=False) if errors else None, run_id,
            ),
        )
