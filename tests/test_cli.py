"""The command line: init-db, ingest, discover, stats end to end against a temp database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.cli import main
from tests.conftest import PROJECT_ROOT


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
database_path: {(tmp_path / "cli.sqlite3").as_posix()}
cache_dir: {(tmp_path / "cache").as_posix()}
migrations_dir: {(PROJECT_ROOT / "migrations").as_posix()}
targets:
  fields:
    software_engineering: [software, engineer]
prefilter:
  exclude_title_keywords: [senior]
classification:
  intern_keywords: [intern, internship]
  new_grad_keywords: [new grad]
http:
  default_min_interval_seconds: 0
  max_retries: 1
  backoff_base_seconds: 0.001
discovery:
  seed_file: null
  common_crawl: {{enabled: false}}
sources:
  greenhouse: {{enabled: true}}
""",
        encoding="utf-8",
    )
    return path


def run_cli(config_path: Path, *arguments: str) -> int:
    return main(["--config", str(config_path), *arguments])


def database(config_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(config_path.parent / "cli.sqlite3")
    connection.row_factory = sqlite3.Row
    return connection


def test_init_db_is_repeatable(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(config_path, "init-db") == 0
    assert "Applied migrations: 001" in capsys.readouterr().out
    assert run_cli(config_path, "init-db") == 0
    assert "already up to date" in capsys.readouterr().out


def test_stats_on_an_empty_database(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(config_path, "stats") == 0
    assert "Jobs: 0 open, 0 closed" in capsys.readouterr().out


def test_missing_config_exits_with_error_code(tmp_path: Path) -> None:
    assert main(["--config", str(tmp_path / "nope.yaml"), "stats"]) == 2


def test_discover_then_ingest_then_stats(
    config_path: Path, tmp_path: Path, router: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text("companies:\n  - {ats: greenhouse, slug: acme, name: Acme}\n", encoding="utf-8")
    router.get(url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/acme/jobs.*").mock(
        return_value=httpx.Response(200, json={"jobs": [
            {"id": 1, "title": "Software Engineer, New Grad", "location": {"name": "Hyderabad, India"},
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "content": "&lt;p&gt;Class of 2027 welcome&lt;/p&gt;",
             "company_name": "Acme"},
            {"id": 2, "title": "Senior Software Engineer", "location": {"name": "Remote"}},
            {"id": 3, "title": "Software Engineering Intern", "location": {"name": "Remote - US"},
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/3"},
        ]}))

    assert run_cli(config_path, "discover", "--seed", str(seed), "--no-common-crawl") == 0
    out = capsys.readouterr().out
    assert "seed" in out and "promoted=1" in out

    assert run_cli(config_path, "ingest") == 0
    out = capsys.readouterr().out
    assert "greenhouse" in out
    with database(config_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert connection.execute("SELECT status FROM companies").fetchone()[0] == "active"

    assert run_cli(config_path, "stats") == 0
    report = capsys.readouterr().out
    assert "Jobs: 2 open" in report
    assert "greenhouse" in report and "IN" in report and "new_grad" in report and "intern" in report


def test_ingest_unknown_source_is_a_usage_error(config_path: Path) -> None:
    assert run_cli(config_path, "ingest", "--source", "workday") == 2


def test_ingest_limit_caps_companies(config_path: Path, router: respx.MockRouter, capsys: pytest.CaptureFixture[str]) -> None:
    run_cli(config_path, "init-db")
    with database(config_path) as connection:
        for slug in ("a", "b", "c"):
            connection.execute(
                "INSERT INTO companies (name, ats_type, slug, status, created_at) VALUES (?, 'greenhouse', ?, 'active', 't')",
                (slug, slug),
            )
    route = router.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(return_value=httpx.Response(200, json={"jobs": []}))
    assert run_cli(config_path, "ingest", "--limit", "2") == 0
    assert route.call_count == 2
