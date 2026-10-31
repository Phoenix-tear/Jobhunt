"""Remote eligibility: who is a remote job actually open to? (table-driven, offline)"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from jobhunt.matching.remote import (
    PARSER_VERSION,
    RemoteDeriver,
    RemoteHints,
    RemoteInputs,
    RemoteScope,
    WorkplaceType,
    extract_hints,
    refresh_remote,
    remote_basis,
)

R, H, O, U = WorkplaceType.REMOTE, WorkplaceType.HYBRID, WorkplaceType.ONSITE, WorkplaceType.UNKNOWN
WW, REG, CTRY, UNK = RemoteScope.WORLDWIDE, RemoteScope.REGIONAL, RemoteScope.COUNTRY_RESTRICTED, RemoteScope.UNKNOWN


@pytest.fixture(scope="module")
def deriver() -> RemoteDeriver:
    return RemoteDeriver(home_country="IN")


# (location text, workplace, scope, regions, eligible for India)
LOCATION_CASES: list[tuple[str, WorkplaceType, RemoteScope, tuple[str, ...], bool | None]] = [
    ("Remote", R, UNK, (), None),
    ("REMOTE", R, UNK, (), None),
    ("Remote - US", R, CTRY, ("US",), False),
    ("Remote (US)", R, CTRY, ("US",), False),
    ("REMOTE (US)", R, CTRY, ("US",), False),
    ("US Remote", R, CTRY, ("US",), False),
    ("Remote US", R, CTRY, ("US",), False),
    ("Remote in USA", R, CTRY, ("US",), False),
    ("Remote, United States", R, CTRY, ("US",), False),
    ("United States (Remote)", R, CTRY, ("US",), False),
    ("United States | Remote", R, CTRY, ("US",), False),
    ("Remote - India", R, CTRY, ("IN",), True),
    ("India (remote)", R, CTRY, ("IN",), True),
    ("Remote - Europe", R, REG, ("EUROPE",), False),
    ("Remote (EMEA)", R, REG, ("EMEA",), False),
    ("Remote EU", R, REG, ("EUROPE",), False),
    ("Remote - APAC", R, REG, ("APAC",), True),
    ("Remote - Asia", R, REG, ("APAC",), True),
    ("Remote - Southeast Asia", R, REG, ("APAC",), True),
    ("Remote - Americas", R, REG, ("AMER",), False),
    ("Remote - LATAM", R, REG, ("LATAM",), False),
    ("Remote (EST)", R, REG, ("NA",), False),
    ("Remote - Middle East", R, REG, ("EMEA",), False),
    ("Remote - Canada", R, CTRY, ("CA",), False),
    ("Remote Canada", R, CTRY, ("CA",), False),
    ("Remote (Peru)", R, CTRY, ("PE",), False),
    ("Remote in UK", R, CTRY, ("GB",), False),
    ("United Kingdom - Remote", R, CTRY, ("GB",), False),
    ("Remote, India or US", R, CTRY, ("IN", "US"), True),
    ("Anywhere", R, WW, (), True),
    ("Worldwide", R, WW, (), True),
    ("Remote - Worldwide", R, WW, (), True),
    ("Remote (Global)", R, WW, (), True),
    ("Remote - Worldwide (excluding US)", R, WW, (), True),
    ("Remote - Worldwide excluding India", R, WW, (), False),
    ("Amsterdam, Netherlands; Remote - Europe", R, REG, ("EUROPE",), False),
    ("San Francisco, CA, US; Remote, US", R, CTRY, ("US",), False),
    ("Santa Clara, CA or Remote", R, CTRY, ("US",), False),
    ("Cardiff, London or Remote (UK)", R, CTRY, ("GB",), False),
    ("Tokyo (Remote/Hybrid)", R, CTRY, ("JP",), False),
    ("Remote / Boston / Salt Lake City / San Francisco / New York", R, CTRY, ("US",), False),
    ("Homeoffice", R, UNK, (), None),
    ("Remote International", R, UNK, (), None),
    ("Seattle; Remote", R, UNK, (), None),            # hubs alone do not say who may apply
    ("Hybrid - London", H, UNK, (), None),
    ("Draper, UT (Hybrid)", H, UNK, (), None),
    ("Hybrid Remote/SF Bay Area/Austin/Boston", H, UNK, (), None),
    ("On-site - Bengaluru", O, UNK, (), None),
    ("Bengaluru, India", U, UNK, (), None),
    ("San Jose, CA", U, UNK, (), None),
    ("", U, UNK, (), None),
]


@pytest.mark.parametrize("location,workplace,scope,regions,eligible", LOCATION_CASES, ids=[c[0] or "<empty>" for c in LOCATION_CASES])
def test_location_strings(deriver: RemoteDeriver, location, workplace, scope, regions, eligible) -> None:
    info = deriver.derive(RemoteInputs(location_raw=location))
    assert info.workplace_type is workplace
    assert info.remote_scope is scope
    assert info.remote_regions == regions
    assert info.remote_eligible_india is eligible


def test_at_least_forty_location_cases() -> None:
    assert len(LOCATION_CASES) >= 40


# --- description phrases ----------------------------------------------------------------------

DESCRIPTION_CASES: list[tuple[str, str | None, str, WorkplaceType, RemoteScope, tuple[str, ...], bool | None, bool]] = [
    ("Remote", None, "You must be located in the United States to be considered.", R, CTRY, ("US",), False, False),
    ("Remote", None, "This role is open to candidates based in India.", R, CTRY, ("IN",), True, False),
    ("Remote", None, "Candidates must be residing within the European Union.", R, REG, ("EUROPE",), False, False),
    ("Remote", None, "We hire globally; this is a remote position worldwide.", R, WW, (), True, False),
    ("Remote", None, "Work from anywhere in the world with our async team.", R, WW, (), True, False),
    ("", None, "This is a fully remote role. Applicants must be authorized to work in the United States.", R, CTRY, ("US",), False, True),
    ("Berlin, Germany", None, "We are unable to provide visa sponsorship for this position.", U, UNK, (), None, True),
    ("Remote", None, "US-based candidates only. We do not sponsor visas.", R, CTRY, ("US",), False, True),
    ("", "Backend Engineer (Remote - EMEA)", "Join us.", R, REG, ("EMEA",), False, False),
    ("", "Software Engineer - Remote (India)", "Join us.", R, CTRY, ("IN",), True, False),
    ("", None, "This role is hybrid, with 3 days in the office each week.", H, UNK, (), None, False),
    ("", None, "This position is on-site at our Austin headquarters.", O, UNK, (), None, False),
    ("Pune, India", None, "Experience with hybrid cloud architecture is a plus.", U, UNK, (), None, False),
    ("Boulder, CO", "Remote Sensing Engineer", "Satellite imagery.", U, UNK, (), None, False),
    ("Pune", "Distributed Systems Engineer", "Build services.", U, UNK, (), None, False),
    ("Remote", None, "Please note: we cannot offer sponsorship at this time.", R, UNK, (), None, True),
    ("Remote", None, "Must have the right to work in the UK.", R, CTRY, ("GB",), False, True),
    # Company boilerplate is not a statement about this role, and a posting that names its own
    # place is not re-scoped by a description sentence.
    ("Bengaluru, India", None, "We are a fully remote company. Candidates must be located in the United States for our US team.",
     U, UNK, (), None, False),
    ("Berlin, Germany", None, "Planet is a global company with employees working remotely world wide and joining us from offices.",
     U, UNK, (), None, False),
    ("Bengaluru, India", None, "This role is remote. You must be located in the United States.", R, UNK, (), None, False),
    ("Remoto", None, "Buscamos ingenieros.", R, UNK, (), None, False),
]


@pytest.mark.parametrize("location,title,description,workplace,scope,regions,eligible,work_auth", DESCRIPTION_CASES)
def test_description_and_title_evidence(
    deriver: RemoteDeriver, location, title, description, workplace, scope, regions, eligible, work_auth
) -> None:
    info = deriver.derive(RemoteInputs(title=title, location_raw=location, description=description))
    assert (info.workplace_type, info.remote_scope, info.remote_regions, info.remote_eligible_india) == (
        workplace, scope, regions, eligible
    )
    assert info.work_auth_required is work_auth


def test_structured_beats_text_and_text_beats_description(deriver: RemoteDeriver) -> None:
    hybrid_flag = RemoteHints(workplace=H)
    info = deriver.derive(RemoteInputs(location_raw="Remote - US", description="fully remote", hints=hybrid_flag))
    assert info.workplace_type is H and info.source == "structured"
    info = deriver.derive(RemoteInputs(location_raw="Remote - India", description="must be located in the United States"))
    assert info.remote_regions == ("IN",) and info.source == "text"


def test_unclear_never_becomes_a_guess(deriver: RemoteDeriver) -> None:
    """Posting country alone is not a restriction: stays NULL (unclear), evidence mentions it."""
    hints = RemoteHints(workplace=R, posting_country="US")
    info = deriver.derive(RemoteInputs(location_raw="San Francisco", hints=hints))
    assert info.workplace_type is R
    assert info.remote_scope is UNK and info.remote_eligible_india is None
    assert "posting country US" in info.evidence


def test_home_country_is_configurable() -> None:
    us_user = RemoteDeriver(home_country="US")
    assert us_user.derive(RemoteInputs(location_raw="Remote - US")).remote_eligible_india is True
    assert us_user.derive(RemoteInputs(location_raw="Remote - India")).remote_eligible_india is False
    assert us_user.derive(RemoteInputs(location_raw="Remote - APAC")).remote_eligible_india is False


# --- structured payload shapes ----------------------------------------------------------------

RAW_CASES: list[tuple[str, dict[str, Any], str | None, WorkplaceType, RemoteScope, tuple[str, ...], bool | None]] = [
    ("ashby hybrid (isRemote is true for hybrid)", {"workplaceType": "Hybrid", "isRemote": True, "location": "San Francisco"},
     "San Francisco", H, UNK, (), None),
    ("ashby remote, hub only", {"workplaceType": "Remote", "isRemote": True, "location": "San Francisco"},
     "San Francisco", R, UNK, (), None),
    ("ashby remote with country", {"workplaceType": "Remote", "isRemote": True}, "Remote - Germany", R, CTRY, ("DE",), False),
    ("ashby onsite", {"workplaceType": "OnSite", "isRemote": False}, "Hyderabad, India", O, UNK, (), None),
    ("ashby unspecified", {"workplaceType": None, "isRemote": None}, "Hyderabad, India", U, UNK, (), None),
    ("ashby flag only", {"workplaceType": None, "isRemote": True}, "Remote - India", R, CTRY, ("IN",), True),
    ("lever remote", {"categories": {"location": "Remote - US", "allLocations": ["Remote - US"]}, "workplaceType": "remote", "country": "US"},
     "Remote - US", R, CTRY, ("US",), False),
    ("lever onsite", {"categories": {"location": "Dubai"}, "workplaceType": "onsite", "country": "AE"}, "Dubai", O, UNK, (), None),
    ("lever unspecified", {"categories": {"location": "Dubai"}, "workplaceType": "unspecified"}, "Dubai", U, UNK, (), None),
    ("recruitee hybrid", {"remote": False, "hybrid": True, "on_site": False, "country_code": "NL"}, "Utrecht", H, UNK, (), None),
    ("recruitee remote", {"remote": True, "hybrid": False, "on_site": False}, "Remote", R, UNK, (), None),
    ("region-only location on a remote posting", {"categories": {"location": "LATAM"}, "workplaceType": "remote"},
     "LATAM", R, REG, ("LATAM",), False),
    ("country-only location on a remote posting", {"categories": {"location": "India"}, "workplaceType": "remote"},
     "India", R, CTRY, ("IN",), True),
    ("workable telecommuting", {"telecommuting": True, "country": "India"}, "India", R, CTRY, ("IN",), True),
    ("smartrecruiters remote", {"customField": [], "location": {"remote": True, "hybrid": False, "country": "us"}},
     "Remote", R, UNK, (), None),
    ("himalayas worldwide", {"locationRestrictions": [], "timezoneRestrictions": [], "title": "x"}, None, R, WW, (), True),
    ("himalayas US only", {"locationRestrictions": ["United States"], "timezoneRestrictions": [-5]}, None, R, CTRY, ("US",), False),
    ("himalayas India and US", {"locationRestrictions": ["India", "United States"]}, None, R, CTRY, ("IN", "US"), True),
    ("remotive worldwide", {"candidate_required_location": "Worldwide", "category": "x"}, None, R, WW, (), True),
    ("remotive USA only", {"candidate_required_location": "USA Only", "category": "x"}, None, R, CTRY, ("US",), False),
    ("weworkremotely anywhere", {"region": "Anywhere in the World", "guid": "g", "category": "x"}, None, R, WW, (), True),
    ("jobicy APAC", {"jobGeo": "APAC", "jobTitle": "x"}, None, R, REG, ("APAC",), True),
    ("jobicy UK and USA", {"jobGeo": "UK,  USA", "jobTitle": "x"}, None, R, CTRY, ("GB", "US"), False),
    ("jobicy anywhere", {"jobGeo": "Anywhere", "jobTitle": "x"}, None, R, WW, (), True),
    ("remoteok blank location", {"epoch": 1, "location": "", "position": "x"}, None, R, UNK, (), None),
    ("arbeitnow remote", {"job_types": [], "remote": True, "location": "Remote"}, "Remote", R, UNK, (), None),
    ("greenhouse metadata remote", {"offices": [], "metadata": [{"name": "Workplace Type", "value": "Remote"}]},
     "Austin, TX", R, UNK, (), None),
    ("greenhouse metadata hybrid+onsite list", {"offices": [], "metadata": [{"name": "Workplace Type", "value": ["Hybrid", "On-site"]}]},
     "Austin, TX", H, UNK, (), None),
    ("greenhouse metadata onsite phrase", {"offices": [], "metadata": [{"name": "Workplace Type", "value": "Collectibles Onsite 5 Days"}]},
     "Austin, TX", O, UNK, (), None),
    ("greenhouse metadata null", {"offices": [], "metadata": [{"name": "Workplace Type", "value": None}]},
     "Austin, TX", U, UNK, (), None),
]


@pytest.mark.parametrize("label,raw,location,workplace,scope,regions,eligible", RAW_CASES, ids=[c[0] for c in RAW_CASES])
def test_structured_payloads(deriver: RemoteDeriver, label, raw, location, workplace, scope, regions, eligible) -> None:
    hints = extract_hints(raw)
    info = deriver.derive(RemoteInputs(location_raw=location, hints=hints))
    assert (info.workplace_type, info.remote_scope, info.remote_regions, info.remote_eligible_india) == (
        workplace, scope, regions, eligible
    ), info.evidence


@pytest.mark.parametrize("bad", [None, [], "text", 7, {"metadata": "nope"}, {"workplaceType": 5, "isRemote": "x"}])
def test_hostile_payloads_never_raise(bad: Any) -> None:
    assert isinstance(extract_hints(bad), RemoteHints)


def test_very_long_and_odd_input_is_safe(deriver: RemoteDeriver) -> None:
    info = deriver.derive(RemoteInputs(location_raw="Remote - " + "x" * 50_000, description="remote " * 20_000))
    assert info.workplace_type is R
    deriver.derive(RemoteInputs(location_raw="\u0000‮;;||\n", title="\U0001f600", description="\x00" * 100))


# --- applying it to the database --------------------------------------------------------------


def _insert(connection: sqlite3.Connection, n: int, **values: Any) -> int:
    row = {
        "fingerprint": f"fp{n}", "title": "Engineer", "company_name": "Acme", "location_raw": "Remote - US",
        "first_seen_at": "2026-10-01T00:00:00Z", "last_seen_at": "2026-10-01T00:00:00Z", "status": "open",
        "description_text": "desc", "description_hash": f"h{n}",
    }
    row.update(values)
    cursor = connection.execute(
        f"INSERT INTO jobs ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values())
    )
    return int(cursor.lastrowid)


def _read(connection: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    return connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def test_refresh_stores_fields_and_is_incremental(connection: sqlite3.Connection, deriver: RemoteDeriver) -> None:
    us = _insert(connection, 1, location_raw="Remote - US")
    india = _insert(connection, 2, location_raw="Remote - India")
    plain = _insert(connection, 3, location_raw="Pune, India")
    ashby = _insert(connection, 4, location_raw="San Francisco",
                    raw_json=json.dumps({"workplaceType": "Hybrid", "isRemote": True, "location": "San Francisco"}))

    stats = refresh_remote(connection, deriver, "IN")
    assert stats.updated == 4 and stats.examined == 4

    row = _read(connection, us)
    assert (row["workplace_type"], row["remote_scope"], json.loads(row["remote_regions"]), row["remote_eligible_india"]) == (
        "remote", "country_restricted", ["US"], 0)
    assert _read(connection, india)["remote_eligible_india"] == 1
    assert _read(connection, plain)["workplace_type"] == "unknown" and _read(connection, plain)["remote_eligible_india"] is None
    assert _read(connection, ashby)["workplace_type"] == "hybrid"     # isRemote=true must not win over workplaceType
    assert row["remote_basis"] == remote_basis("IN", "h1", "Remote - US", None)
    assert f"v{PARSER_VERSION}" in row["remote_basis"]

    again = refresh_remote(connection, deriver, "IN")
    assert again.updated == 0 and again.skipped_current == 4

    connection.execute("UPDATE jobs SET location_raw = 'Remote - Canada' WHERE id = ?", (us,))
    changed = refresh_remote(connection, deriver, "IN")
    assert changed.updated == 1
    assert json.loads(_read(connection, us)["remote_regions"]) == ["CA"]

    forced = refresh_remote(connection, deriver, "IN", force=True)
    assert forced.updated == 4


def test_llm_settled_rows_survive_force_but_not_changed_inputs(connection: sqlite3.Connection, deriver: RemoteDeriver) -> None:
    job = _insert(connection, 1, location_raw="Remote")
    refresh_remote(connection, deriver, "IN")
    connection.execute("UPDATE jobs SET remote_eligible_india = 1, remote_source = 'llm' WHERE id = ?", (job,))

    refresh_remote(connection, deriver, "IN", force=True)
    row = _read(connection, job)
    assert row["remote_eligible_india"] == 1 and row["remote_source"] == "llm"

    connection.execute("UPDATE jobs SET description_hash = 'new-hash' WHERE id = ?", (job,))
    refresh_remote(connection, deriver, "IN")
    row = _read(connection, job)
    assert row["remote_eligible_india"] is None and row["remote_source"] != "llm"


def test_schema_is_additive(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
    assert {"workplace_type", "remote_scope", "remote_regions", "remote_eligible_india", "work_auth_required"} <= columns
    job = _insert(connection, 9)
    row = _read(connection, job)
    assert row["workplace_type"] == "unknown" and row["remote_scope"] == "unknown" and row["work_auth_required"] == 0
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE jobs SET workplace_type = 'moon' WHERE id = ?", (job,))


# --- overnight: description / label patterns for workplace type ------------------------------------

from jobhunt.matching.remote import RemoteDeriver, RemoteInputs, WorkplaceType  # noqa: E402


@pytest.mark.parametrize("description,expected", [
    ("Location: Bolivia. Work Mode: Remote. We are hiring.", WorkplaceType.REMOTE),
    ("Location: McLean, VA | Work Model: On-site (5 days/week) Are you ready", WorkplaceType.ONSITE),
    ("Working Pattern: Hybrid (typically 3 days per week onsite)", WorkplaceType.HYBRID),
    ("Great team. #LI-Hybrid #LI-CH1", WorkplaceType.HYBRID),
    ("This position requires in office presence. The base salary", WorkplaceType.ONSITE),
    ("This role requires you to be onsite. Remote and/or hybrid work will not be considered.", WorkplaceType.ONSITE),
    ("Hybrid work setup (up to 2 remote days per week)", WorkplaceType.HYBRID),
    ("We are in person 5 days a week, with one company-wide remote week", WorkplaceType.ONSITE),
    ("We currently ask that individuals spend 3 to 4 days a week on average working in the office", WorkplaceType.HYBRID),
    ("Expect to be in the office 2 days a week.", WorkplaceType.HYBRID),
    ("Location: Hybrid 3 days in office (New York, NY)", WorkplaceType.HYBRID),
])
def test_description_patterns_resolve_workplace(description, expected):
    deriver = RemoteDeriver()
    info = deriver.derive(RemoteInputs(title="Engineer", location_raw="Somewhere, USA", description=description))
    assert info.workplace_type == expected


@pytest.mark.parametrize("description", [
    "We do not offer hybrid or remote options for this internship.",
    "Experience with hybrid cloud infrastructure and remote access tooling.",
    "No remote work is available; the team meets 3 days a week for standup calls.",
    "We are a remote-first company with offices in several cities.",
])
def test_negated_or_unrelated_phrases_do_not_set_workplace(description):
    deriver = RemoteDeriver()
    info = deriver.derive(RemoteInputs(title="Engineer", location_raw="Somewhere, USA", description=description))
    assert info.workplace_type == WorkplaceType.UNKNOWN


def test_parser_upgrade_keeps_an_llm_verdict_when_the_posting_is_unchanged(
        connection: sqlite3.Connection, deriver: RemoteDeriver) -> None:
    from jobhunt.matching.remote import PARSER_VERSION

    job = _insert(connection, 1, location_raw="Remote", description_hash="h1", workplace_type="remote",
                  remote_eligible_india=1, remote_source="llm", remote_basis=f"v{PARSER_VERSION - 1}|IN|h1|Remote|")
    stats = refresh_remote(connection, deriver, "IN")
    row = _read(connection, job)
    assert stats.skipped_llm == 1 and row["remote_source"] == "llm" and row["remote_eligible_india"] == 1
