"""Fixtures for the website API tests: a temp database (migrations 001+002) and a TestClient."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobhunt.api.app import create_app
from jobhunt.config import Config
from jobhunt.db import apply_migrations, connect

NOW_ISO = "2026-10-12T09:00:00Z"


@pytest.fixture()
def api_config(tmp_path: Path, config: Config) -> Config:
    """The real config pointed at a throw-away database; email token files live in tmp_path."""
    email = dataclasses.replace(
        config.email, token_file=str(tmp_path / "token.json"), credentials_file=str(tmp_path / "credentials.json")
    )
    profile_tmp = tmp_path / "profile"
    profile_tmp.mkdir(parents=True, exist_ok=True)
    profiles = dataclasses.replace(config.profiles, profile_dir=str(profile_tmp))
    return dataclasses.replace(config, database_path=str(tmp_path / "api.sqlite3"), email=email,
                               profiles=profiles, user_agent_contact="https://example.test/me")


@pytest.fixture()
def db(api_config: Config) -> Iterator[sqlite3.Connection]:
    connection = connect(api_config.database_file, check_same_thread=False)
    apply_migrations(connection, api_config.migrations_directory)
    yield connection
    connection.close()


@pytest.fixture()
def make_client(api_config: Config, db: sqlite3.Connection, tmp_path: Path) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _make(config: Config | None = None, **kwargs: Any) -> TestClient:
        app = create_app(config or api_config, extra_allowed_hosts=("testserver",), frontend_dir=tmp_path / "no-dist", **kwargs)
        token = getattr(app.state, "csrf_token", "")
        client = TestClient(app, headers={"X-Jobhunt-Token": token})
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture()
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


# --- seeding helpers --------------------------------------------------------------------------------

_counter = {"n": 0}


def add_job(connection: sqlite3.Connection, **overrides: Any) -> int:
    """Insert one job row with sensible defaults; returns its id. Sources: pass `sources=[(name, url), ...]`."""
    _counter["n"] += 1
    n = _counter["n"]
    sources = overrides.pop("sources", [("greenhouse", f"https://boards.greenhouse.io/acme/jobs/{n}")])
    values: dict[str, Any] = {
        "fingerprint": f"fp-{n}", "canonical_apply_url": f"https://boards.greenhouse.io/acme/jobs/{n}",
        "title": f"Software Engineer {n}", "company_name": "Acme", "location_raw": "Bengaluru, India",
        "country": "IN", "city": "Bengaluru", "is_remote": 0, "employment_type": "full_time",
        "experience_level": "entry", "description_text": f"Build things number {n}.", "needs_description": 0,
        "posted_at": f"2026-10-{(n % 9) + 1:02d}T08:00:00Z", "first_seen_at": "2026-10-10T00:00:00Z",
        "last_seen_at": NOW_ISO, "status": "open",
    }
    values.update(overrides)
    columns = ", ".join(values)
    cursor = connection.execute(
        f"INSERT INTO jobs ({columns}) VALUES ({', '.join('?' for _ in values)})", list(values.values())
    )
    job_id = int(cursor.lastrowid)
    for index, (source, url) in enumerate(sources):
        connection.execute(
            """INSERT INTO job_sightings (job_id, source, source_job_id, source_url, attribution_text, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, '2026-10-10T00:00:00Z', ?)""",
            (job_id, source, f"{source}-{n}-{index}", url, f"via {source}", NOW_ISO),
        )
    return job_id


def titles(response_json: dict[str, Any]) -> list[str]:
    return [item["title"] for item in response_json["items"]]


def add_relevance(connection: sqlite3.Connection, job_id: int, status: str = "target", **overrides: Any) -> None:
    """Attach a stored relevance verdict (and optional scores) to a job."""
    values: dict[str, Any] = {
        "job_id": job_id, "computed_at": NOW_ISO, "rules_hash": "r", "input_hash": "i", "gate_status": status,
        "gate_rule": "title_ai_ml" if status != "excluded" else "hard_exclude_title",
        "gate_reason": "Title says ML." if status != "excluded" else "Title names an excluded role.",
    }
    values.update(overrides)
    connection.execute(
        f"INSERT INTO job_relevance ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
        list(values.values()),
    )
