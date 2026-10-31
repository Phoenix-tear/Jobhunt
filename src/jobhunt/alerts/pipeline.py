"""`ingest-email`: read job-alert emails, store the parsed jobs, hand ATS links to the ATS adapters.

Rules this module enforces (each has a test):
* only messages from the per-platform sender allowlist are parsed (re-checked after fetching);
* `processed_emails` makes reruns idempotent; a message is recorded only once its jobs are stored;
* only parsed fields are persisted: titles, companies, locations, links. Never a message body;
* links on LinkedIn/Naukri/Indeed/... are never requested. A link that already points at a
  supported ATS is the one exception: its company is added to discovery and the posting is
  fetched through the ATS's public API (the full description then merges by URL).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jobhunt.alerts.gmail import MailSource, RawEmail, sender_allowed
from jobhunt.alerts.parsing import ParsedAlertJob, parse_alert_html, parse_alert_text
from jobhunt.classify import Classifier, TitlePrefilter
from jobhunt.companies import DiscoveredCompany, add_discovered_companies
from jobhunt.config import Config
from jobhunt.db import transaction
from jobhunt.dedupe import upsert_jobs
from jobhunt.discovery.probe import probe_unverified
from jobhunt.errors import FetchError
from jobhunt.http import HttpClient
from jobhunt.lifecycle import close_stale_feed_jobs
from jobhunt.locations import LocationParser
from jobhunt.models import EmploymentType, NormalizedJob
from jobhunt.normalize import enrich_job
from jobhunt.runner import IngestionRunner
from jobhunt.sources import ATS_SOURCE_NAMES
from jobhunt.sources.base import JobSource
from jobhunt.state import get_cursor, record_source_run, set_cursor
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)

SOURCE_STATE_KEY = "gmail"
_QUERY_OVERLAP = timedelta(days=1)   # re-list a day back; processed_emails absorbs the duplicates
_ID_CHUNK = 400


def email_source_name(platform: str) -> str:
    return f"email_{platform}"


@dataclass
class EmailRunSummary:
    messages_listed: int = 0
    messages_new: int = 0
    messages_skipped_sender: int = 0
    jobs_parsed: int = 0
    jobs_new: int = 0
    jobs_merged: int = 0
    jobs_skipped_by_prefilter: int = 0
    jobs_closed: int = 0
    companies_discovered: int = 0
    ats_companies_resolved: int = 0
    errors: list[str] = field(default_factory=list)


def build_query(config: Config, after: datetime) -> str:
    """Gmail search: any allowlisted sender, newer than `after`."""
    terms: list[str] = []
    for settings in config.email.platforms.values():
        if settings.enabled:
            terms.extend(entry.lstrip("@") for entry in settings.senders)
    if not terms:
        return ""
    unique = " OR ".join(dict.fromkeys(terms))
    return f"from:({unique}) after:{int(after.timestamp())}"


def match_platform(config: Config, sender: str) -> str | None:
    for platform, settings in config.email.platforms.items():
        if settings.enabled and sender_allowed(sender, settings.senders):
            return platform
    return None


def parse_email(platform: str, message: RawEmail, locations: LocationParser) -> list[ParsedAlertJob]:
    """HTML body first; plain text as a fallback. Never touches the network."""
    jobs = parse_alert_html(platform, message.html, locations) if message.html else []
    if not jobs and message.text:
        jobs = parse_alert_text(platform, message.text, locations)
    return jobs


def ingest_emails(
    connection: sqlite3.Connection,
    config: Config,
    mailbox: MailSource,
    *,
    http: HttpClient | None = None,
    ats_sources: Mapping[str, JobSource] | None = None,
    clock: Clock = utc_now,
    resolve_ats: bool | None = None,
    since_days: int | None = None,
    limit: int | None = None,
) -> EmailRunSummary:
    settings = config.email
    summary = EmailRunSummary()
    now = clock()
    prefilter = TitlePrefilter.from_config(config)
    classifier = Classifier.from_config(config)
    locations = LocationParser(config.classification.extra_city_countries)

    stored_cursor = get_cursor(connection, SOURCE_STATE_KEY)
    if since_days is not None or not (stored_cursor and stored_cursor.isdigit()):
        after = now - timedelta(days=since_days if since_days is not None else settings.lookback_days)
    else:
        after = datetime.fromtimestamp(int(stored_cursor), tz=now.tzinfo) - _QUERY_OVERLAP

    query = build_query(config, after)
    if not query:
        summary.errors.append("no enabled platform has any sender configured")
        return summary

    message_ids = mailbox.list_message_ids(query, limit or settings.max_messages_per_run)
    summary.messages_listed = len(message_ids)
    processed = _already_processed(connection, message_ids)
    new_ids = [message_id for message_id in message_ids if message_id not in processed]
    summary.messages_new = len(new_ids)

    discovered: list[DiscoveredCompany] = []
    # The stored position only ever moves forward, even when the window was widened with --since-days.
    stored_epoch = int(stored_cursor) if stored_cursor and stored_cursor.isdigit() else 0
    newest_epoch = max(stored_epoch, int(after.timestamp()))

    for message_id in new_ids:
        try:
            message = mailbox.get_message(message_id)
        except FetchError as error:
            summary.errors.append(f"{message_id}: {error}")
            continue
        platform = match_platform(config, message.sender)
        if message.received_at is not None:
            newest_epoch = max(newest_epoch, int(message.received_at.timestamp()))

        if platform is None:
            summary.messages_skipped_sender += 1
            _record_processed(connection, message, 0, now)
            continue

        try:
            parsed = parse_email(platform, message, locations)
        except Exception as error:  # a parser bug must not stop the batch; the message is retried next run
            logger.exception("parsing message %s failed", message_id)
            summary.errors.append(f"{message_id}: parser error {type(error).__name__}")
            continue

        jobs: list[NormalizedJob] = []
        message_companies: list[DiscoveredCompany] = []
        skipped = 0
        for item in parsed:
            if not prefilter.keeps(item.title):
                skipped += 1
                continue
            jobs.append(_to_job(item, message, platform, locations, classifier))
            if item.ats is not None:
                message_companies.append(DiscoveredCompany(
                    ats_type=item.ats.ats_type.value, slug=item.ats.slug, region=item.ats.region,
                    discovered_via=f"email:{platform}",
                ))

        with transaction(connection):
            stats = upsert_jobs(connection, jobs, company_id=None, seen_at=to_iso(message.received_at or now))
            summary.companies_discovered += add_discovered_companies(connection, message_companies, now)
            _record_processed(connection, message, len(parsed), now)
        discovered.extend(message_companies)
        summary.jobs_parsed += len(parsed)
        summary.jobs_new += stats.created
        summary.jobs_merged += stats.merged
        summary.jobs_skipped_by_prefilter += skipped

    with transaction(connection):
        cutoff = to_iso(now - timedelta(days=settings.stale_after_days))
        for source_name in (email_source_name(platform) for platform in config.email.platforms):
            summary.jobs_closed += close_stale_feed_jobs(connection, source_name, cutoff, to_iso(now), ATS_SOURCE_NAMES)
        set_cursor(connection, SOURCE_STATE_KEY, str(newest_epoch))
        record_source_run(connection, SOURCE_STATE_KEY, now, success=not summary.errors, fetches=len(new_ids) + 1)

    do_resolve = settings.resolve_ats_links if resolve_ats is None else resolve_ats
    if do_resolve and discovered and http is not None and ats_sources:
        summary.ats_companies_resolved = _resolve_ats_links(connection, config, discovered, http, ats_sources, clock)
    return summary


# -- helpers --------------------------------------------------------------------------------------

def _to_job(
    item: ParsedAlertJob, message: RawEmail, platform: str, locations: LocationParser, classifier: Classifier
) -> NormalizedJob:
    job = NormalizedJob(
        source=email_source_name(platform),
        source_job_id=item.job_id or item.url,
        title=item.title,
        company_name=item.company,
        apply_url=item.url,
        source_url=item.url,
        location_raw=item.location,
        needs_description=True,         # an email card never carries the description
        posted_at=to_iso(message.received_at) if message.received_at else None,
        attribution_text=f"Via {platform.title()} job-alert email (link only; the {platform.title()} page was not fetched)",
        raw={"platform": platform, "link_recognized": item.recognized},   # parsed fields only, never the body
    )
    if re.search(r"/internships?/", item.url, re.IGNORECASE):   # Internshala/Unstop: the path says it
        job.employment_type = EmploymentType.INTERNSHIP
    return enrich_job(job, locations, classifier)


def _already_processed(connection: sqlite3.Connection, message_ids: list[str]) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(message_ids), _ID_CHUNK):
        chunk = message_ids[start : start + _ID_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT gmail_message_id FROM processed_emails WHERE gmail_message_id IN ({placeholders})", chunk
        )
        found.update(row[0] for row in rows)
    return found


def _record_processed(connection: sqlite3.Connection, message: RawEmail, job_count: int, now: datetime) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO processed_emails (gmail_message_id, processed_at, sender, parsed_job_count) VALUES (?,?,?,?)",
        (message.message_id, to_iso(now), message.sender, job_count),
    )


def _resolve_ats_links(
    connection: sqlite3.Connection,
    config: Config,
    discovered: list[DiscoveredCompany],
    http: HttpClient,
    ats_sources: Mapping[str, JobSource],
    clock: Clock,
) -> int:
    """Probe the newly seen ATS companies and ingest their boards through the ATS APIs."""
    pairs = {(entry.ats_type, entry.slug.lower()) for entry in discovered if entry.ats_type in ats_sources}
    if not pairs:
        return 0
    rows = connection.execute("SELECT id, ats_type, slug FROM companies").fetchall()
    company_ids: list[int] = [row["id"] for row in rows if (row["ats_type"], row["slug"].lower()) in pairs]
    if not company_ids:
        return 0
    probe_unverified(connection, config, ats_sources, http, clock=clock, company_ids=company_ids)
    runner = IngestionRunner(connection, config, ats_sources, http, clock=clock)
    runner.run(sorted({ats_type for ats_type, _ in pairs}), company_ids=company_ids)
    return len(company_ids)


__all__ = ["EmailRunSummary", "build_query", "email_source_name", "ingest_emails", "match_platform", "parse_email"]
