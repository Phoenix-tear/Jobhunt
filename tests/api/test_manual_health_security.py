"""Manual add (SSRF-safe), health (offline) and the local-only security properties."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from jobhunt.alerts.gmail import READONLY_SCOPE
from jobhunt.cli import build_parser, main
from jobhunt.config import Config
from tests.api.conftest import add_job


@pytest.fixture()
def router() -> respx.MockRouter:
    """Any HTTP request made while this is active and unmocked raises: proof of 'no network'."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock_router:
        yield mock_router


MANUAL = {"title": "Platform Engineer", "company": "Acme", "apply_url": "https://jobs.example.com/careers/platform-engineer-42"}


class TestManualAdd:
    def test_creates_a_job_with_a_manual_sighting(self, client: TestClient, db: sqlite3.Connection, router: respx.MockRouter) -> None:
        response = client.post("/api/jobs/manual", json={**MANUAL, "location": "Bengaluru, India", "description": "Plain text."})
        assert response.status_code == 200
        body = response.json()
        assert body["created"] is True and body["ats"]["status"] == "not_applicable"
        job = body["job"]
        assert (job["title"], job["company_name"], job["country"], job["city"]) == ("Platform Engineer", "Acme", "IN", "Bengaluru")
        assert job["description_text"] == "Plain text." and job["needs_description"] is False
        assert [s["source"] for s in job["sightings"]] == ["manual"] and job["apply_url"] == MANUAL["apply_url"]
        assert router.calls.call_count == 0

    def test_no_description_means_needs_description(self, client: TestClient, router: respx.MockRouter) -> None:
        job = client.post("/api/jobs/manual", json=MANUAL).json()["job"]
        assert job["needs_description"] is True and job["description_text"] is None

    def test_same_url_twice_dedupes(self, client: TestClient, db: sqlite3.Connection) -> None:
        first = client.post("/api/jobs/manual", json=MANUAL).json()
        second = client.post("/api/jobs/manual", json={**MANUAL, "apply_url": MANUAL["apply_url"] + "?utm_source=newsletter#top"}).json()
        assert (first["created"], second["created"]) == (True, False) and first["job"]["id"] == second["job"]["id"]
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM job_sightings").fetchone()[0] == 1

    def test_merges_with_an_existing_job_by_canonical_url(self, client: TestClient, db: sqlite3.Connection, router: respx.MockRouter) -> None:
        existing = add_job(db, title="Existing", canonical_apply_url="https://example.org/jobs/role-123",
                           sources=[("remotive", "https://example.org/jobs/role-123")])
        body = client.post("/api/jobs/manual", json={**MANUAL, "apply_url": "https://www.example.org/jobs/role-123?ref=me"}).json()
        assert body["created"] is False and body["job"]["id"] == existing
        assert {s["source"] for s in body["job"]["sightings"]} == {"remotive", "manual"}

    @pytest.mark.parametrize(
        "url",
        ["javascript:alert(1)", "ftp://example.com/job", "file:///etc/passwd", "example.com/job", "http://", "https:///x",
         "data:text/html,<script>1</script>", "https://exa mple.com/job", "x" * 3000],
    )
    def test_non_http_urls_are_rejected(self, client: TestClient, url: str, router: respx.MockRouter) -> None:
        assert client.post("/api/jobs/manual", json={**MANUAL, "apply_url": url}).status_code == 422
        assert router.calls.call_count == 0

    def test_required_fields(self, client: TestClient) -> None:
        for bad in ({**MANUAL, "title": ""}, {**MANUAL, "company": "   "}, {"title": "x"}, {**MANUAL, "extra": 1}):
            assert client.post("/api/jobs/manual", json=bad).status_code == 422

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com/jobs/view/3912345678",
            "https://in.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b",
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost:8000/api/health",
            "https://evil.example/boards.greenhouse.io/acme/jobs/1",
            "https://boards.greenhouse.io.evil.example/acme/jobs/1",
        ],
    )
    def test_unsupported_hosts_make_no_network_call(self, client: TestClient, router: respx.MockRouter, url: str) -> None:
        body = client.post("/api/jobs/manual", json={**MANUAL, "apply_url": url, "fetch_from_ats": True}).json()
        assert body["ats"] == {"attempted": False, "status": "not_applicable", "detail": None}
        assert router.calls.call_count == 0                       # any request would have raised

    def test_supported_ats_url_is_fetched_through_the_adapter_only(
        self, client: TestClient, db: sqlite3.Connection, router: respx.MockRouter
    ) -> None:
        board = router.get(url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/rocketai/jobs.*").mock(
            return_value=httpx.Response(200, json={"jobs": [
                {"id": 4455667, "title": "Software Engineer, Platform", "company_name": "Rocket AI",
                 "absolute_url": "https://boards.greenhouse.io/rocketai/jobs/4455667", "location": {"name": "Remote - US"},
                 "content": "&lt;p&gt;Full description from the ATS.&lt;/p&gt;", "first_published": "2026-10-01T10:00:00Z"},
                {"id": 1, "title": "Other Job", "absolute_url": "https://boards.greenhouse.io/rocketai/jobs/1"},
            ]}))
        body = client.post("/api/jobs/manual", json={
            "title": "My Title", "company": "Rocket", "apply_url": "https://boards.greenhouse.io/rocketai/jobs/4455667?gh_src=x",
        }).json()
        assert body["ats"]["status"] == "fetched" and board.call_count == 1
        job = body["job"]
        assert "Full description from the ATS." in job["description_text"] and job["needs_description"] is False
        assert {s["source"] for s in job["sightings"]} == {"manual", "greenhouse"}
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1               # merged, not duplicated
        company = db.execute("SELECT status, discovered_via FROM companies WHERE slug = 'rocketai'").fetchone()
        assert (company["status"], company["discovered_via"]) == ("unverified", "manual")
        assert {call.request.url.host for call in router.calls} == {"boards-api.greenhouse.io"}

    def test_ats_failure_still_creates_the_job(self, client: TestClient, router: respx.MockRouter) -> None:
        router.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(return_value=httpx.Response(404, json={}))
        body = client.post("/api/jobs/manual", json={**MANUAL, "apply_url": "https://boards.greenhouse.io/ghost/jobs/5"}).json()
        assert body["created"] is True and body["ats"]["status"] == "not_found"

    def test_server_error_from_the_ats_does_not_become_a_500(self, client: TestClient, router: respx.MockRouter) -> None:
        router.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(return_value=httpx.Response(503))
        response = client.post("/api/jobs/manual", json={**MANUAL, "apply_url": "https://boards.greenhouse.io/acme/jobs/5"})
        assert response.status_code == 200 and response.json()["ats"]["status"] == "failed"

    def test_fetch_can_be_switched_off(self, client: TestClient, db: sqlite3.Connection, router: respx.MockRouter) -> None:
        body = client.post("/api/jobs/manual", json={**MANUAL, "apply_url": "https://jobs.lever.co/acme/0f0c4a52-1a2b-4c3d-8e9f-aabbccddeeff",
                                                    "fetch_from_ats": False}).json()
        assert body["ats"]["status"] == "skipped" and router.calls.call_count == 0
        assert db.execute("SELECT COUNT(*) FROM companies WHERE slug = 'acme'").fetchone()[0] == 1   # still discovered


class TestHealth:
    def test_works_with_no_network_and_counts(self, client: TestClient, db: sqlite3.Connection, router: respx.MockRouter) -> None:
        add_job(db)
        add_job(db, status="closed")
        db.execute("INSERT INTO companies (name, ats_type, slug, status, created_at) VALUES ('A','greenhouse','a','active','t')")
        db.execute("INSERT INTO companies (name, ats_type, slug, status, created_at) VALUES ('B','lever','b','unverified','t')")
        body = client.get("/api/health").json()
        assert (body["jobs_open"], body["jobs_closed"], body["companies_active"], body["companies_unverified"]) == (1, 1, 1, 1)
        from jobhunt.db import discover_migrations

        migrations = Path(__file__).resolve().parents[2] / "migrations"
        assert body["schema_version"] == len(discover_migrations(migrations)) and body["user_agent_contact_set"] is True
        assert router.calls.call_count == 0

    def test_source_freshness_and_last_run(self, client: TestClient, db: sqlite3.Connection) -> None:
        from datetime import timedelta
        from jobhunt.timeutil import to_iso, utc_now

        now = utc_now()
        db.execute("INSERT INTO source_state (source, last_run_at, last_success_at, fetch_count_today) VALUES (?,?,?,?)",
                   ("greenhouse", to_iso(now - timedelta(hours=2)), to_iso(now - timedelta(hours=2)), 12))
        db.execute("INSERT INTO source_state (source, last_run_at, last_success_at) VALUES (?,?,?)",
                   ("remotive", to_iso(now - timedelta(hours=200)), to_iso(now - timedelta(hours=200))))
        db.execute("""INSERT INTO ingestion_runs (source, started_at, finished_at, companies_checked, jobs_seen, jobs_new,
                      jobs_closed, jobs_skipped_by_prefilter, errors_json) VALUES ('greenhouse', ?, ?, 5, 100, 7, 1, 80, ?)""",
                   (to_iso(now), to_iso(now), json.dumps([{"company": "greenhouse/x", "kind": "not_found", "message": "HTTP 404"}])))
        sources = {s["source"]: s for s in client.get("/api/health").json()["sources"]}
        assert sources["greenhouse"]["stale"] is False and sources["greenhouse"]["fetch_count_today"] == 12
        assert sources["greenhouse"]["last_run"]["jobs_new"] == 7 and sources["greenhouse"]["last_run"]["error_count"] == 1
        assert "not_found" in sources["greenhouse"]["last_run"]["first_error"]
        assert sources["remotive"]["stale"] is True and sources["remotive"]["age_hours"] > 190
        assert sources["lever"]["never_run"] is True and sources["lever"]["stale"] is False
        warnings = [w["code"] for w in client.get("/api/health").json()["warnings"]]
        assert "source_stale" in warnings and "source_never_run" in warnings

    def test_missing_contact_and_token_warn(self, make_client: Callable[..., TestClient], api_config: Config) -> None:
        body = make_client(dataclasses.replace(api_config, user_agent_contact="  ")).get("/api/health").json()
        codes = {w["code"] for w in body["warnings"]}
        assert {"user_agent_contact_missing", "gmail_token_missing"} <= codes
        assert body["gmail"]["token_file_exists"] is False and body["user_agent_contact_set"] is False

    def test_read_only_token_is_recognized_without_contacting_google(
        self, make_client: Callable[..., TestClient], api_config: Config, tmp_path: Path, router: respx.MockRouter
    ) -> None:
        (tmp_path / "token.json").write_text(json.dumps({"token": "t", "scopes": [READONLY_SCOPE]}), encoding="utf-8")
        (tmp_path / "credentials.json").write_text("{}", encoding="utf-8")
        body = make_client().get("/api/health").json()
        assert body["gmail"]["read_only_scope"] is True and body["gmail"]["credentials_file_exists"] is True
        assert "gmail_token_missing" not in {w["code"] for w in body["warnings"]}
        assert router.calls.call_count == 0

    def test_wrong_scope_is_flagged(self, make_client: Callable[..., TestClient], tmp_path: Path) -> None:
        (tmp_path / "token.json").write_text(
            json.dumps({"scopes": ["https://www.googleapis.com/auth/gmail.modify"]}), encoding="utf-8")
        body = make_client().get("/api/health").json()
        assert body["gmail"]["read_only_scope"] is False
        assert "gmail_scope_unexpected" in {w["code"] for w in body["warnings"]}

    def test_corrupt_token_file_does_not_break_health(self, make_client: Callable[..., TestClient], tmp_path: Path) -> None:
        (tmp_path / "token.json").write_text("{not json", encoding="utf-8")
        response = make_client().get("/api/health")
        assert response.status_code == 200 and response.json()["gmail"]["scopes"] == []


class TestLocalOnlySecurity:
    def test_foreign_host_header_is_refused(self, client: TestClient) -> None:
        assert client.get("/api/jobs", headers={"host": "evil.example"}).status_code == 400
        assert client.get("/api/jobs", headers={"host": "127.0.0.1:8000"}).status_code == 200
        assert client.get("/api/jobs", headers={"host": "localhost"}).status_code == 200

    def test_cross_origin_writes_are_refused(self, client: TestClient, db: sqlite3.Connection) -> None:
        job_id = add_job(db)
        blocked = client.post("/api/applications", json={"job_id": job_id}, headers={"origin": "https://evil.example"})
        assert blocked.status_code == 403
        assert db.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
        allowed = client.post("/api/applications", json={"job_id": job_id}, headers={"origin": "http://127.0.0.1:5173"})
        assert allowed.status_code == 201

    def test_write_bodies_must_be_json(self, client: TestClient, db: sqlite3.Connection) -> None:
        response = client.post("/api/applications", content="job_id=1", headers={"content-type": "application/x-www-form-urlencoded"})
        assert response.status_code == 415
        response = client.post("/api/applications", content="job_id=1", headers={"content-type": "text/plain"})
        assert response.status_code == 415

    def test_cors_allows_only_the_dev_origin(self, client: TestClient) -> None:
        ok = client.options("/api/jobs", headers={"origin": "http://127.0.0.1:5173", "access-control-request-method": "GET"})
        assert ok.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"
        bad = client.options("/api/jobs", headers={"origin": "https://evil.example", "access-control-request-method": "GET"})
        assert "access-control-allow-origin" not in bad.headers
        simple = client.get("/api/jobs", headers={"origin": "https://evil.example"})
        assert "access-control-allow-origin" not in simple.headers

    def test_serve_binds_to_loopback_and_has_no_host_option(self, api_config: Config, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        import uvicorn

        captured: dict[str, object] = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
        from jobhunt.cli import _command_serve

        args = build_parser().parse_args(["serve", "--port", "8123"])
        assert _command_serve(args, api_config) == 0
        assert captured["host"] == "127.0.0.1" and captured["port"] == 8123
        with pytest.raises(SystemExit):
            build_parser().parse_args(["serve", "--host", "0.0.0.0"])

    def test_serve_automatically_selects_next_port_if_occupied(self, api_config: Config, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        import socket
        import uvicorn

        captured: dict[str, object] = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
        from jobhunt.cli import _command_serve

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 8125))
            args = build_parser().parse_args(["serve", "--port", "8125"])
            assert _command_serve(args, api_config) == 0
            assert captured["host"] == "127.0.0.1" and captured["port"] == 8126
            out = capsys.readouterr().out
            assert "Port 8125 is already in use; listening on port 8126 instead." in out

    def test_serve_port_zero_picks_free_port(self, api_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        import uvicorn

        captured: dict[str, object] = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
        from jobhunt.cli import _command_serve

        args = build_parser().parse_args(["serve", "--port", "0"])
        assert _command_serve(args, api_config) == 0
        assert captured["host"] == "127.0.0.1"
        assert isinstance(captured["port"], int) and captured["port"] > 0

    def test_serve_respects_env_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PORT", "8888")
        args = build_parser().parse_args(["serve"])
        assert args.port == 8888

    def test_static_frontend_falls_back_to_index_for_client_routes(self, api_config: Config, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient as Client
        from jobhunt.api.app import create_app

        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html><title>jobhunt</title>", encoding="utf-8")
        (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        with Client(create_app(api_config, frontend_dir=dist, extra_allowed_hosts=("testserver",))) as app_client:
            assert "jobhunt" in app_client.get("/tracker").text                  # SPA route -> index.html
            assert app_client.get("/assets/app.js").text == "console.log(1)"
            assert app_client.get("/api/jobs").headers["content-type"].startswith("application/json")
            assert app_client.get("/api/does-not-exist").status_code == 404     # API misses are real 404s, not index.html


def test_main_serve_requires_no_database_content(api_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"database_path: {(tmp_path / 'fresh.sqlite3').as_posix()}\nmigrations_dir: {api_config.migrations_directory.as_posix()}\n",
        encoding="utf-8",
    )
    assert main(["--config", str(config_file), "serve", "--port", "8124"]) == 0
