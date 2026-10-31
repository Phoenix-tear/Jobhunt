"""Shared fixtures. Nothing in the default test run touches the network."""

from __future__ import annotations

import dataclasses
import json
import shutil
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import respx

os.environ["JOBHUNT_TERMS_FILE"] = ""  # never read the owner's data/profile/terms.yaml in tests

import jobhunt.config as jobhunt_config
from jobhunt.config import Config, load_config
from jobhunt.db import apply_migrations, connect
from jobhunt.http import HttpClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live-smoke",
        action="store_true",
        default=False,
        help="run tests marked 'live' against real public endpoints",
    )
    parser.addoption(
        "--live-gemini",
        action="store_true",
        default=False,
        help="run tests marked 'live_gemini' against the real Gemini API (a few tiny calls, uses GEMINI_API_KEY)",
    )
    parser.addoption(
        "--real-db-smoke",
        action="store_true",
        default=False,
        help="run tests marked 'realdb' against the real database (they clean up after themselves)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_live = pytest.mark.skip(reason="needs --live-smoke (hits real public endpoints)")
    skip_real_db = pytest.mark.skip(reason="needs --real-db-smoke (reads and temporarily writes the real database)")
    skip_gemini = pytest.mark.skip(reason="needs --live-gemini (calls the real Gemini API with GEMINI_API_KEY)")
    for item in items:
        if "live_gemini" in item.keywords and not config.getoption("--live-gemini"):
            item.add_marker(skip_gemini)
        if "live" in item.keywords and not config.getoption("--live-smoke"):
            item.add_marker(skip_live)
        if "realdb" in item.keywords and not config.getoption("--real-db-smoke"):
            item.add_marker(skip_real_db)


_real_load_config = jobhunt_config.load_config
_current_data_dir: list[Path] = []


def _hermetic_load_config(path: str | Path | None = None, **kwargs):  # noqa: ANN003, ANN202
    """The checkout's own config.yaml always gets a throw-away data dir, so tests never read or write
    the git-ignored data/ (profile, DB, settings). Configs built in a test's tmp_path are left alone."""
    config_path = Path(path) if path else Path.cwd() / "config.yaml"
    if kwargs.get("data_dir") is None and not os.environ.get("JOBHUNT_DATA_DIR") and _current_data_dir and config_path.resolve() == (PROJECT_ROOT / "config.yaml").resolve():
        kwargs["data_dir"] = _current_data_dir[-1]
    return _real_load_config(path, **kwargs)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A fresh empty data dir per test for the checkout's config; patches every `load_config` reference."""
    data_dir = tmp_path_factory.mktemp("data-dir")
    _current_data_dir.append(data_dir)
    for module in list(sys.modules.values()):
        if getattr(module, "load_config", None) is _real_load_config:
            monkeypatch.setattr(module, "load_config", _hermetic_load_config)
    yield data_dir
    _current_data_dir.pop()


@pytest.fixture(scope="session")
def config(tmp_path_factory: pytest.TempPathFactory) -> Config:
    """The shipped config.yaml, rooted in a throw-away mirror of the checkout (config, migrations, seeds).

    Tests never see personal overrides and never touch the checkout's git-ignored data/: everything the
    code resolves under `data/` lands in the mirror."""
    mirror = tmp_path_factory.mktemp("project")
    for name in ("config.yaml", "enterprise_companies.yaml"):
        if (PROJECT_ROOT / name).is_file():
            shutil.copy2(PROJECT_ROOT / name, mirror / name)
    for folder in ("migrations", "seeds"):
        if (PROJECT_ROOT / folder).is_dir():
            shutil.copytree(PROJECT_ROOT / folder, mirror / folder)
    return load_config(mirror / "config.yaml", use_local=False)


@pytest.fixture()
def connection(tmp_path: Path, config: Config) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "test.sqlite3")
    apply_migrations(conn, config.migrations_directory)
    yield conn
    conn.close()


class FakeClock:
    """Injectable clock: tests move time explicitly instead of sleeping."""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> datetime:
        self.current += timedelta(**delta)
        return self.current


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def sleeps() -> list[float]:
    """Every delay the HTTP client *would* have slept; tests assert on this instead of waiting."""
    return []


@pytest.fixture()
def http_client(config: Config, sleeps: list[float]) -> Iterator[HttpClient]:
    settings = dataclasses.replace(
        config.http, max_retries=2, default_min_interval_seconds=0.0, host_min_interval_seconds={}
    )
    client = HttpClient(settings, "jobhunt-tests/0", sleep=sleeps.append, jitter=lambda: 0.0)
    yield client
    client.close()


@pytest.fixture()
def router() -> Iterator[respx.MockRouter]:
    """Mocked HTTP: any request without a registered route fails the test (no real network)."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock_router:
        yield mock_router


@pytest.fixture()
def load_fixture() -> Callable[[str], object]:
    def _load(relative_path: str) -> object:
        return json.loads((FIXTURE_DIR / relative_path).read_text(encoding="utf-8"))

    return _load
