"""Location policy: location_fit verdicts, multi-location postings, config, scoring and the batch run."""

from __future__ import annotations

import dataclasses
import json
import sqlite3

import pytest

from jobhunt.config import Config, LocationSettings
from jobhunt.matching.location import (
    ABROAD_UNKNOWN, INDIA, REMOTE_OPEN, REMOTE_UNCLEAR, UNKNOWN, WRONG, LocationClassifier, run_location,
)
from jobhunt.matching.remote import RemoteDeriver, RemoteInputs
from tests.test_scoring import add_job


@pytest.fixture(scope="module")
def classifier(config: Config) -> LocationClassifier:
    return LocationClassifier(config)


def verdict(classifier: LocationClassifier, config: Config, location: str | None, *, country: str | None = None,
            workplace: str | None = None, eligible: int | None = None, work_auth: bool = False):
    """Fill workplace / eligibility with the remote parser the way `backfill-remote` would."""
    if workplace is None:
        derived = RemoteDeriver(home_country=config.user.home_country).derive(RemoteInputs(location_raw=location))
        workplace = derived.workplace_type.value
        eligible = None if derived.remote_eligible_india is None else int(derived.remote_eligible_india)
        regions = list(derived.remote_regions)
    else:
        regions = []
    return classifier.classify(country=country, location_raw=location, workplace=workplace, eligible=eligible,
                               scope=None, regions=regions, work_auth=work_auth)


@pytest.mark.parametrize("location,country,expected", [
    # india: any workplace type
    ("Bengaluru, Karnataka, India", "IN", INDIA),
    ("Hyderabad", None, INDIA),
    ("Gurugram", None, INDIA),
    ("Noida, UP", None, INDIA),
    ("Pune (Hybrid)", None, INDIA),
    ("Mumbai - On-site", None, INDIA),
    ("Remote - India", None, INDIA),
    ("Delhi NCR", None, INDIA),
    # multi-location: any qualifying location qualifies the job
    ("Bengaluru; London; Remote - US", None, INDIA),
    ("London | Chennai | New York", None, INDIA),
    ("San Francisco; Remote - Worldwide", None, REMOTE_OPEN),
    # remote open to India
    ("Anywhere", None, REMOTE_OPEN),
    ("Remote (Worldwide)", None, REMOTE_OPEN),
    ("Remote - APAC", None, REMOTE_OPEN),
    ("Remote - Asia", None, REMOTE_OPEN),
    ("Worldwide", None, REMOTE_OPEN),
    # remote but unclear
    ("Remote", None, REMOTE_UNCLEAR),
    # wrong location
    ("Remote - US", "US", WRONG),
    ("Remote - Canada", "CA", WRONG),
    ("Remote - Europe", None, WRONG),
    ("EMEA only", None, WRONG),
    ("Remote - United Kingdom", "GB", WRONG),
    ("New York, NY (On-site)", "US", WRONG),
    ("London, UK - Hybrid", "GB", WRONG),
    # abroad, workplace unknown
    ("Berlin, Germany", "DE", ABROAD_UNKNOWN),
    ("San Francisco, CA", "US", ABROAD_UNKNOWN),
    # no information
    (None, None, UNKNOWN),
    ("", None, UNKNOWN),
])
def test_location_fit_table(classifier: LocationClassifier, config: Config, location, country, expected) -> None:
    result = verdict(classifier, config, location, country=country)
    assert result.fit == expected, (location, result)
    assert result.reason


def test_the_matched_location_is_recorded(classifier: LocationClassifier, config: Config) -> None:
    result = verdict(classifier, config, "London; Pune; Remote - US")
    assert result.fit == INDIA and result.matched == "Pune"


def test_remote_with_only_a_foreign_city_is_not_open_to_india(classifier: LocationClassifier, config: Config) -> None:
    result = classifier.classify(country="US", location_raw="Austin, TX", workplace="remote", eligible=None,
                                 scope=None, regions=[], work_auth=False)
    assert result.fit == REMOTE_UNCLEAR            # remote, scope not stated: visible with a badge
    result = classifier.classify(country="US", location_raw="Austin, TX", workplace="remote", eligible=0,
                                 scope="country_restricted", regions=["NA"], work_auth=False)
    assert result.fit == WRONG and "NA" in result.reason


def test_work_authorization_downgrades_a_remote_job_to_unclear(classifier: LocationClassifier) -> None:
    result = classifier.classify(country=None, location_raw="Remote", workplace="remote", eligible=1, scope="worldwide",
                                 regions=[], work_auth=True)
    assert result.fit == REMOTE_UNCLEAR and "work authorization" in result.reason


def test_config_changes_the_policy_retroactively(config: Config) -> None:
    strict = dataclasses.replace(config, location=LocationSettings(accept_workplace_in_india=("remote",),
                                                                   accept_abroad=()))
    classifier = LocationClassifier(strict)
    onsite = classifier.classify(country="IN", location_raw="Pune", workplace="onsite", eligible=None, scope=None,
                                 regions=[], work_auth=False)
    assert onsite.fit == WRONG
    remote_abroad = classifier.classify(country=None, location_raw="Remote - Worldwide", workplace="remote",
                                        eligible=1, scope="worldwide", regions=[], work_auth=False)
    assert remote_abroad.fit == WRONG


def test_run_location_updates_rows_and_feeds_the_final_score(connection: sqlite3.Connection, config: Config) -> None:
    from datetime import datetime, timezone

    from jobhunt.matching.scoring import compute_final_scores

    rows = {
        "india": add_job(connection, 1, "ML Engineer", "Python.", country="IN", location_raw="Pune", workplace_type="onsite"),
        "wrong": add_job(connection, 2, "ML Engineer B", "Python.", country="US", location_raw="Austin", workplace_type="onsite"),
        "unknown": add_job(connection, 3, "ML Engineer C", "Python.", country=None, location_raw=None),
    }
    for job_id in rows.values():
        connection.execute(
            """INSERT INTO job_relevance (job_id, computed_at, rules_hash, input_hash, gate_status, gate_rule, gate_reason,
                   similarity_ai, similarity_fullstack, similarity_percentile) VALUES (?, 'x', 'r', 'i', 'target', 'r', 'r',
                   0.7, 0.6, 0.5)""", (job_id,))
    stats = run_location(connection, config)
    assert stats.by_fit == {"india": 1, "wrong_location": 1, "unknown": 1}
    compute_final_scores(connection, config, clock=lambda: datetime(2026, 10, 14, tzinfo=timezone.utc))
    scores = {r["job_id"]: json.loads(r["score_components"]) for r in connection.execute(
        "SELECT job_id, score_components FROM job_relevance")}
    assert scores[rows["india"]]["location_score"] == config.scoring.location.home_country_onsite
    assert scores[rows["unknown"]]["location_score"] == config.scoring.location.unknown
    assert scores[rows["wrong"]]["location_score"] == config.scoring.location.remote_restricted_elsewhere


def test_gemini_pool_only_contains_jobs_that_pass_the_location_policy(connection: sqlite3.Connection,
                                                                      config: Config) -> None:
    from datetime import date

    from jobhunt.matching.scoring import _scoring_pool

    cfg = dataclasses.replace(config, scoring=dataclasses.replace(config.scoring, recent_days=3650))
    good = add_job(connection, 10, "ML Engineer", "Python.", country="IN", location_raw="Pune")
    bad = add_job(connection, 11, "ML Engineer 2", "Python.", country="US", location_raw="Austin", workplace_type="onsite")
    for job_id in (good, bad):
        connection.execute(
            """INSERT INTO job_relevance (job_id, computed_at, rules_hash, input_hash, gate_status, gate_rule, gate_reason,
                   similarity_ai) VALUES (?, 'x', 'r', 'i', 'target', 'r', 'r', 0.7)""", (job_id,))
    run_location(connection, cfg)
    ids = {job.job_id for job in _scoring_pool(connection, cfg, date(2026, 10, 14))}
    assert good in ids and bad not in ids
