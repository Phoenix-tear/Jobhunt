"""Location policy: where is this job, and is it somewhere I want?

`location_fit` is independent of the role gate and fully reversible (nothing is deleted):

  india                     located in India, any workplace type (subject to `location.accept_workplace_in_india`)
  remote_open_to_india      remote and open to someone living in India (worldwide / APAC / India / unrestricted)
  remote_unclear            remote, but who may apply is not stated (or work authorization is required): check
  abroad_unknown_workplace  abroad with an unknown workplace type: hidden by default, not a hard exclusion
  wrong_location            abroad and on-site/hybrid, or remote restricted to non-India countries / regions
  unknown                   no location information at all: kept visible

Evidence order: structured fields (stored country, workplace type, eligibility from the remote parser), then the
location text (every segment of "Bengaluru; London; Remote - US" is judged: ANY qualifying segment qualifies the
job and is recorded as the matched location), then the existing remote parser's description-derived verdict.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from jobhunt.config import Config
from jobhunt.locations import LocationParser
from jobhunt.matching.remote import RemoteDeriver, RemoteInputs, WorkplaceType
from jobhunt.matching.triage import effect_of
from jobhunt.matching.triage_apply import usable_verdicts

logger = logging.getLogger(__name__)

INDIA = "india"
REMOTE_OPEN = "remote_open_to_india"
REMOTE_UNCLEAR = "remote_unclear"
ABROAD_UNKNOWN = "abroad_unknown_workplace"
WRONG = "wrong_location"
UNKNOWN = "unknown"
FITS = (INDIA, REMOTE_OPEN, REMOTE_UNCLEAR, ABROAD_UNKNOWN, WRONG, UNKNOWN)
# What "relevant locations" means in the default list view.
DEFAULT_VISIBLE = (INDIA, REMOTE_OPEN, REMOTE_UNCLEAR, UNKNOWN)

_SPLIT = re.compile(r"\s*[;|•·]\s*|\s+/\s+|\n+")
_COUNTRY_NAMES = {"US": "the US", "GB": "the UK", "CA": "Canada", "DE": "Germany", "FR": "France", "AU": "Australia"}


@dataclass(frozen=True)
class LocationVerdict:
    fit: str
    reason: str
    matched: str | None = None


def split_locations(location_raw: str | None) -> list[str]:
    return [part.strip() for part in _SPLIT.split(location_raw or "") if part.strip()]


class LocationClassifier:
    def __init__(self, config: Config, parser: LocationParser | None = None, profile: Any | None = None) -> None:
        from jobhunt.profile import get_profile

        p = profile or get_profile(config)
        home = p.location_policy.home_country if p else config.user.home_country
        from jobhunt.locations import _COUNTRIES
        home_iso = _COUNTRIES.get(home.strip().lower(), home.strip().upper())
        self._parser = parser or LocationParser(config.classification.extra_city_countries)
        self._deriver = RemoteDeriver(self._parser, home_iso)
        self._home = home_iso
        self._home_names = {home_iso, home.strip().upper()}
        settings = config.location
        if profile is not None:
            self._india_workplaces = {v.lower() for v in profile.location_policy.workplace_types}
        elif config is not None and config.location.accept_workplace_in_india != ("remote", "hybrid", "onsite"):
            self._india_workplaces = {value.lower() for value in config.location.accept_workplace_in_india}
        elif p is not None:
            self._india_workplaces = {v.lower() for v in p.location_policy.workplace_types}
        else:
            self._india_workplaces = {value.lower() for value in settings.accept_workplace_in_india}
        self._abroad_ok = {value.lower() for value in settings.accept_abroad}

    def classify(self, *, country: str | None, location_raw: str | None, workplace: str, eligible: int | None,
                 scope: str | None, regions: list[str], work_auth: bool) -> LocationVerdict:
        workplace = (workplace or "unknown").lower()
        segments = split_locations(location_raw)
        parsed = [(segment, self._parser.parse(segment)) for segment in segments]

        # 1. in India (stored country, or any segment names an Indian place)
        india_segment = None
        if (country or "").upper() in self._home_names:
            india_segment = (location_raw or "").strip() or self._home
        else:
            for segment, place in parsed:
                if any(h in place.all_countries for h in self._home_names) or (place.country or "").upper() in self._home_names:
                    india_segment = segment
                    break
        if india_segment is not None:
            if workplace in {"unknown", ""} or workplace in self._india_workplaces:
                return LocationVerdict(INDIA, f"located in India ({workplace})", india_segment)
            return LocationVerdict(WRONG, f"{workplace} in India is not accepted by location.accept_workplace_in_india",
                                   india_segment)

        # 2. a segment that is itself remote and open to India ("Remote - APAC", "Anywhere")
        for segment, _place in parsed:
            derived = self._deriver.derive(RemoteInputs(location_raw=segment))
            if derived.workplace_type is WorkplaceType.REMOTE and derived.remote_eligible_india is True:
                return self._remote_open(segment, work_auth)

        # 3. job-level remote verdict from the remote parser (structured fields, description patterns)
        if workplace == "remote":
            if eligible == 1:
                return self._remote_open(location_raw or "remote", work_auth)
            if eligible == 0:
                where = ", ".join(regions) if regions else self._where(country, parsed)
                return LocationVerdict(WRONG, f"remote, restricted to {where or 'other countries'}", location_raw)
            return LocationVerdict(REMOTE_UNCLEAR, "remote, but who may apply is not stated: check the remote scope",
                                   location_raw)

        # 3b. a bare region restriction without a workplace word ("EMEA only", "Europe", "North America")
        for segment, place in parsed:
            if place.country:
                continue
            flagged = self._deriver.derive(RemoteInputs(location_raw=segment, is_remote_flag=True))
            if flagged.remote_eligible_india is False:
                where = ", ".join(flagged.remote_regions) or segment
                return LocationVerdict(WRONG, f"restricted to {where}", segment)

        # 4. abroad
        abroad_country = (country or "").upper() or next(
            (place.country for _, place in parsed if place.country and place.country.upper() not in self._home_names), None)
        if abroad_country:
            label = _COUNTRY_NAMES.get(abroad_country.upper(), abroad_country.upper())
            if workplace in {"onsite", "hybrid"}:
                return LocationVerdict(WRONG, f"{workplace} in {label}", location_raw)
            return LocationVerdict(ABROAD_UNKNOWN, f"in {label}, workplace type unknown", location_raw)
        if location_raw and location_raw.strip():
            return LocationVerdict(UNKNOWN, "location text did not name a known country", location_raw)
        return LocationVerdict(UNKNOWN, "no location information", None)

    def _remote_open(self, matched: str, work_auth: bool) -> LocationVerdict:
        if work_auth:
            return LocationVerdict(REMOTE_UNCLEAR, "remote and unrestricted by region, but work authorization is "
                                                   "required: check the remote scope", matched)
        if "remote_open_to_india" not in self._abroad_ok:
            return LocationVerdict(WRONG, "remote jobs abroad are not accepted by location.accept_abroad", matched)
        return LocationVerdict(REMOTE_OPEN, "remote and open to candidates in India", matched)

    @staticmethod
    def _where(country: str | None, parsed: list[tuple[str, Any]]) -> str:
        if country:
            return _COUNTRY_NAMES.get(country.upper(), country.upper())
        countries = [place.country for _, place in parsed if place.country]
        return ", ".join(_COUNTRY_NAMES.get(c, c) for c in dict.fromkeys(countries))


def _student_location_rule(triage_json: str) -> str:
    try:
        rules = json.loads(triage_json).get("rules") or []
    except (ValueError, AttributeError):
        return ""
    return next((str(rule.get("rule", "")) for rule in rules if rule.get("target") == "not_india"), "")


@dataclass
class LocationStats:
    evaluated: int = 0
    by_fit: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.by_fit = self.by_fit or {}


def run_location(connection: sqlite3.Connection, config: Config, *, only_open: bool = True, profile: Any | None = None) -> LocationStats:
    """(Re)compute location_fit for every judged job. Cheap and pure; config changes apply retroactively."""
    classifier = LocationClassifier(config, profile=profile)
    verdicts = usable_verdicts(connection, config)
    where = "WHERE j.status = 'open'" if only_open else ""
    rows = connection.execute(
        f"""SELECT j.id, j.country, j.location_raw, j.workplace_type, j.remote_eligible_india, j.remote_scope,
                   j.remote_regions, j.work_auth_required, r.triage_json
            FROM jobs j JOIN job_relevance r ON r.job_id = j.id {where}""").fetchall()
    stats = LocationStats()
    updates: list[tuple[str, str, str | None, int]] = []
    for row in rows:
        try:
            regions = json.loads(row["remote_regions"]) if row["remote_regions"] else []
        except ValueError:
            regions = []
        verdict = classifier.classify(
            country=row["country"], location_raw=row["location_raw"], workplace=row["workplace_type"] or "unknown",
            eligible=row["remote_eligible_india"], scope=row["remote_scope"],
            regions=[str(item) for item in regions] if isinstance(regions, list) else [],
            work_auth=bool(row["work_auth_required"]))
        stored = verdicts.get(int(row["id"]))
        if stored is not None and verdict.fit in (REMOTE_UNCLEAR, ABROAD_UNKNOWN, UNKNOWN):
            effect = effect_of(stored.verdict, config.triage)
            if effect.location == "hidden":        # verified: the posting restricts the job to places that exclude India
                verdict = LocationVerdict(WRONG, f"llm_location: {effect.location_quote}"[:200], effect.location_quote[:120])
        if stored is None and verdict.fit in (REMOTE_UNCLEAR, ABROAD_UNKNOWN, UNKNOWN) and row["triage_json"]:
            rule = _student_location_rule(row["triage_json"])
            if rule:                               # a mined rule (>= 90% precise, never matches my protected jobs)
                verdict = LocationVerdict(WRONG, f"student_rule: {rule}"[:200], rule[:120])
        updates.append((verdict.fit, verdict.reason[:200], (verdict.matched or "")[:120] or None, int(row["id"])))
        stats.by_fit[verdict.fit] = stats.by_fit.get(verdict.fit, 0) + 1
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.executemany(
            "UPDATE job_relevance SET location_fit = ?, location_reason = ?, location_matched = ? WHERE job_id = ?",
            updates)
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
    stats.evaluated = len(updates)
    return stats
