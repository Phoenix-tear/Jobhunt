"""Discovery: URL patterns, snowball, seeds, Common Crawl, openroles, liveness probe."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.config import CommonCrawlSettings, Config, CrawlPattern, OpenRolesSettings
from jobhunt.db import transaction
from jobhunt.discovery.commoncrawl import CommonCrawlDiscovery
from jobhunt.discovery.openroles import fetch_openroles_slugs
from jobhunt.discovery.patterns import AtsReference, extract_ats_reference, is_valid_slug
from jobhunt.discovery.pipeline import run_discovery
from jobhunt.discovery.probe import probe_unverified
from jobhunt.discovery.seeds import companies_from_urls, load_seed_file
from jobhunt.errors import ConfigError
from jobhunt.http import HttpClient
from jobhunt.models import AtsType
from jobhunt.sources import build_ats_sources
from tests.conftest import FakeClock
from tests.helpers import insert_company, make_job
from jobhunt.dedupe import upsert_jobs

A = AtsType


class TestUrlPatterns:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://boards.greenhouse.io/stripe", AtsReference(A.GREENHOUSE, "stripe")),
            ("https://boards.greenhouse.io/stripe/jobs/8172510", AtsReference(A.GREENHOUSE, "stripe", job_id="8172510")),
            ("https://job-boards.greenhouse.io/stripe/jobs/1?gh_src=x", AtsReference(A.GREENHOUSE, "stripe", job_id="1")),
            ("https://boards.greenhouse.io/embed/job_board?for=airbnb", AtsReference(A.GREENHOUSE, "airbnb")),
            ("https://boards-api.greenhouse.io/v1/boards/cloudflare/jobs/55", AtsReference(A.GREENHOUSE, "cloudflare", job_id="55")),
            ("https://jobs.lever.co/spotify", AtsReference(A.LEVER, "spotify", region="us")),
            ("https://jobs.lever.co/spotify/0f0c4a52-1a2b-4c3d-8e9f-aabbccddeeff/apply",
             AtsReference(A.LEVER, "spotify", region="us", job_id="0f0c4a52-1a2b-4c3d-8e9f-aabbccddeeff")),
            ("https://jobs.eu.lever.co/acme", AtsReference(A.LEVER, "acme", region="eu")),
            ("https://api.eu.lever.co/v0/postings/acme?mode=json", AtsReference(A.LEVER, "acme", region="eu")),
            ("https://jobs.ashbyhq.com/openai", AtsReference(A.ASHBY, "openai")),
            ("https://jobs.ashbyhq.com/openai/7458d4e9-da2e-47bd-98cb-adfda43d42b2",
             AtsReference(A.ASHBY, "openai", job_id="7458d4e9-da2e-47bd-98cb-adfda43d42b2")),
            ("https://apply.workable.com/huggingface/", AtsReference(A.WORKABLE, "huggingface")),
            ("https://apply.workable.com/huggingface/j/81B46579FE/", AtsReference(A.WORKABLE, "huggingface", job_id="81B46579FE")),
            ("https://acme.workable.com/", AtsReference(A.WORKABLE, "acme")),
            ("https://jobs.smartrecruiters.com/BoschGroup", AtsReference(A.SMARTRECRUITERS, "BoschGroup")),
            ("https://jobs.smartrecruiters.com/BoschGroup/744000155007949-brand-manager",
             AtsReference(A.SMARTRECRUITERS, "BoschGroup", job_id="744000155007949")),
            ("https://careers.smartrecruiters.com/oneclick-ui/company/ServiceNow/publication/abc",
             AtsReference(A.SMARTRECRUITERS, "ServiceNow")),
            ("https://bunq.recruitee.com/", AtsReference(A.RECRUITEE, "bunq")),
            ("https://bunq.recruitee.com/o/deputy-aml-manager", AtsReference(A.RECRUITEE, "bunq", job_id="deputy-aml-manager")),
        ],
    )
    def test_extraction(self, url: str, expected: AtsReference) -> None:
        assert extract_ats_reference(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            None, "", "not a url", "https://www.linkedin.com/jobs/view/123", "https://example.com/stripe",
            "https://boards.greenhouse.io/", "https://boards.greenhouse.io/embed/job_board",
            "https://apply.workable.com/j/81B46579FE", "https://apply.workable.com/api/v1/widget/accounts/x",
            "https://www.workable.com/", "https://www.recruitee.com/", "https://api.recruitee.com/x",
            "https://a.b.recruitee.com/", "mailto:jobs@lever.co", "https://jobs.lever.co/../etc",
        ],
    )
    def test_non_boards_are_rejected(self, url: str | None) -> None:
        assert extract_ats_reference(url) is None

    @pytest.mark.parametrize(("slug", "valid"), [("stripe", True), ("a-b_c.d", True), ("", False), ("-x", False),
                                                  ("jobs", False), ("embed", False), ("a/b", False), ("x" * 101, False)])
    def test_slug_validity(self, slug: str, valid: bool) -> None:
        assert is_valid_slug(slug) is valid

    def test_url_with_unicode_or_encoded_slug_does_not_crash(self) -> None:
        assert extract_ats_reference("https://jobs.lever.co/caf%C3%A9") is None
        assert extract_ats_reference("https://jobs.lever.co/%E2%80%A6") is None


class TestSeedsAndSnowball:
    def test_seed_file_is_parsed_and_invalid_entries_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "companies.yaml"
        path.write_text(
            "companies:\n"
            "  - {ats: greenhouse, slug: stripe, name: Stripe}\n"
            "  - {ats: lever, slug: spotify, region: eu}\n"
            "  - {ats: taleo, slug: nope}\n"
            "  - {ats: ashby, slug: 'bad slug!'}\n"
            "  - just a string\n", encoding="utf-8")
        entries = load_seed_file(path)
        assert [(e.ats_type, e.slug, e.region, e.discovered_via) for e in entries] == [
            ("greenhouse", "stripe", None, "seed"), ("lever", "spotify", "eu", "seed"),
        ]

    def test_bad_seed_files_raise_config_errors(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_seed_file(tmp_path / "missing.yaml")
        wrong = tmp_path / "wrong.yaml"
        wrong.write_text("companies: 5\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="companies"):
            load_seed_file(wrong)

    def test_shipped_seed_file_is_valid(self, config: Config) -> None:
        entries = load_seed_file(config.resolve_path("seeds/companies.yaml"))
        assert len(entries) >= 10

    def test_snowball_from_apply_urls_dedupes_and_ignores_other_hosts(self) -> None:
        urls = [
            "https://boards.greenhouse.io/stripe/jobs/1", "https://boards.greenhouse.io/Stripe/jobs/2",
            "https://jobs.lever.co/spotify/abc", "https://www.linkedin.com/jobs/view/42", None,
            "https://acme.recruitee.com/o/dev", "https://stripe.com/jobs?gh_jid=5",
        ]
        found = list(companies_from_urls(urls, "snowball:adzuna"))
        assert [(c.ats_type, c.slug.lower()) for c in found] == [
            ("greenhouse", "stripe"), ("lever", "spotify"), ("recruitee", "acme"),
        ]
        assert {c.discovered_via for c in found} == {"snowball:adzuna"}

    def test_snowball_step_reads_urls_stored_from_any_source(self, connection: sqlite3.Connection, config: Config, http_client: HttpClient, clock: FakeClock) -> None:
        with transaction(connection):
            upsert_jobs(connection, [
                make_job(source="adzuna", source_job_id="a1", apply_url="https://jobs.ashbyhq.com/notion/7458d4e9-da2e-47bd-98cb-adfda43d42b2"),
                make_job(source="hn", source_job_id="h1", title="Backend Engineer", apply_url="https://boards.greenhouse.io/figma/jobs/9"),
                make_job(source="email", source_job_id="e1", title="Data Engineer", apply_url="https://www.naukri.com/job/123"),
            ], company_id=None, seen_at="2026-10-01T00:00:00Z")
        summary = run_discovery(connection, config, {}, http_client, clock=clock, use_seeds=False,
                                use_common_crawl=False, use_openroles=False, use_snowball=True, do_probe=False)
        assert summary.added["snowball"] == 2
        rows = {(r["ats_type"], r["slug"]): r["status"] for r in connection.execute("SELECT * FROM companies")}
        assert rows == {("ashby", "notion"): "unverified", ("greenhouse", "figma"): "unverified"}


CRAWL = "CC-MAIN-2026-39"
INDEX_URL = r"https://index\.commoncrawl\.org/CC-MAIN-2026-39-index.*"


def cdx_lines(*urls: str) -> str:
    return "\n".join(json.dumps({"url": url}) for url in urls)


@pytest.fixture()
def crawl_settings() -> CommonCrawlSettings:
    return CommonCrawlSettings(
        enabled=True, crawls_to_query=1, max_pages_per_pattern=2,
        patterns=(CrawlPattern("greenhouse", "boards.greenhouse.io/*"), CrawlPattern("lever", "jobs.lever.co/*")),
    )


@pytest.fixture()
def mock_crawl(router: respx.MockRouter):
    pages: dict[tuple[str, int], str] = {
        ("boards.greenhouse.io/*", 0): cdx_lines(
            "https://boards.greenhouse.io/stripe/jobs/1", "https://boards.greenhouse.io/stripe/jobs/2",
            "https://boards.greenhouse.io/embed/job_board?for=airbnb", "https://boards.greenhouse.io/favicon.ico",
        ),
        ("boards.greenhouse.io/*", 1): cdx_lines("https://boards.greenhouse.io/Stripe", "https://boards.greenhouse.io/cloudflare/jobs/3", "{broken json"),
        ("boards.greenhouse.io/*", 2): cdx_lines("https://boards.greenhouse.io/page-three-never-fetched"),
        ("jobs.lever.co/*", 0): cdx_lines("https://jobs.lever.co/spotify/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "https://example.com/not-an-ats/page"),
    }
    page_counts = {"boards.greenhouse.io/*": 3, "jobs.lever.co/*": 1}
    router.get("https://index.commoncrawl.org/collinfo.json").mock(
        return_value=httpx.Response(200, json=[{"id": CRAWL, "name": "September 2026 Index"}, {"id": "CC-MAIN-2026-34"}])
    )

    def cdx(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        pattern = params["url"]
        if params.get("showNumPages") == "true":
            return httpx.Response(200, text=json.dumps({"pages": page_counts[pattern], "pageSize": 5, "blocks": 1}))
        return httpx.Response(200, text=pages.get((pattern, int(params["page"])), ""))

    return router.get(url__regex=INDEX_URL).mock(side_effect=cdx)


class TestCommonCrawl:
    def test_slugs_are_extracted_deduped_and_attributed(self, mock_crawl, crawl_settings, http_client, tmp_path) -> None:
        found = list(CommonCrawlDiscovery(crawl_settings, http_client, tmp_path).discover())
        assert [(c.ats_type, c.slug.lower()) for c in found] == [
            ("greenhouse", "stripe"), ("greenhouse", "airbnb"), ("greenhouse", "cloudflare"), ("lever", "spotify"),
        ]
        assert {c.discovered_via for c in found} == {f"commoncrawl:{CRAWL}"}

    def test_page_cap_limits_requests_to_the_index(self, mock_crawl, crawl_settings, http_client, tmp_path) -> None:
        list(CommonCrawlDiscovery(crawl_settings, http_client, tmp_path).discover())
        requested = [(c.request.url.params["url"], c.request.url.params.get("page")) for c in mock_crawl.calls]
        assert ("boards.greenhouse.io/*", "2") not in requested     # capped at 2 pages
        assert sum(1 for _, page in requested if page is not None) == 3   # gh page 0,1 + lever page 0

    def test_second_run_is_served_from_the_disk_cache(self, mock_crawl, crawl_settings, http_client, tmp_path, router) -> None:
        discovery = CommonCrawlDiscovery(crawl_settings, http_client, tmp_path)
        first = list(discovery.discover())
        calls_after_first = mock_crawl.call_count
        second = list(CommonCrawlDiscovery(crawl_settings, http_client, tmp_path).discover())
        assert second == first and mock_crawl.call_count == calls_after_first

    def test_index_outage_degrades_gracefully(self, router, crawl_settings, http_client, tmp_path) -> None:
        router.get("https://index.commoncrawl.org/collinfo.json").mock(
            return_value=httpx.Response(200, json=[{"id": CRAWL}])
        )
        router.get(url__regex=INDEX_URL).mock(return_value=httpx.Response(503))
        assert list(CommonCrawlDiscovery(crawl_settings, http_client, tmp_path).discover()) == []

    def test_uses_a_descriptive_user_agent_and_https(self, mock_crawl, crawl_settings, http_client, tmp_path) -> None:
        list(CommonCrawlDiscovery(crawl_settings, http_client, tmp_path).discover())
        request = mock_crawl.calls[0].request
        assert request.url.scheme == "https" and request.headers["user-agent"].startswith("jobhunt")


class TestOpenRoles:
    def test_only_live_valid_slugs_are_taken(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        entries = [
            {"ats": "greenhouse", "slug": "stripe", "status": "live"},
            {"ats": "greenhouse", "slug": "0x", "status": "dead"},
            {"ats": "greenhouse", "slug": "embed", "status": "live"},   # reserved word
            {"ats": "greenhouse", "slug": "bad slug", "status": "live"},
            "not a dict",
        ]
        router.get(url__regex=r"https://raw\.githubusercontent\.com/datascry/openroles/main/data/tenants/greenhouse\.json").mock(
            return_value=httpx.Response(200, json=entries))
        router.get(url__regex=r"https://raw\.githubusercontent\.com/.*").mock(return_value=httpx.Response(404))
        found = list(fetch_openroles_slugs(OpenRolesSettings(enabled=True), http_client))
        assert [(c.ats_type, c.slug, c.discovered_via) for c in found] == [("greenhouse", "stripe", "openroles")]

    def test_garbage_responses_are_skipped(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        router.get(url__regex=r"https://raw\.githubusercontent\.com/.*").mock(return_value=httpx.Response(200, text="<html>nope</html>"))
        assert list(fetch_openroles_slugs(OpenRolesSettings(enabled=True), http_client)) == []


class TestProbe:
    BOARD = {
        "greenhouse": r"https://boards-api\.greenhouse\.io/v1/boards/{slug}/jobs.*",
        "lever": r"https://api\.lever\.co/v0/postings/{slug}.*",
        "smartrecruiters": r"https://api\.smartrecruiters\.com/v1/companies/{slug}/postings.*",
    }

    def probe(self, connection, config, http_client, clock):
        return probe_unverified(connection, config, build_ats_sources(config), http_client, clock=clock)

    def test_live_board_is_promoted_to_active(self, connection, config, http_client, clock, router) -> None:
        company = insert_company(connection, "greenhouse", "stripe", name="Stripe", status="unverified")
        router.get(url__regex=self.BOARD["greenhouse"].format(slug="stripe")).mock(
            return_value=httpx.Response(200, json={"jobs": [{"id": 1, "title": "Engineer", "company_name": "Stripe, Inc."}]}))
        summary = self.probe(connection, config, http_client, clock)
        row = connection.execute("SELECT * FROM companies WHERE id = ?", (company.id,)).fetchone()
        assert (summary.probed, summary.promoted) == (1, 1)
        assert (row["status"], row["name"], row["last_ok_at"] is not None) == ("active", "Stripe, Inc.", True)
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0   # probing stores no jobs

    def test_valid_empty_board_is_promoted(self, connection, config, http_client, clock, router) -> None:
        insert_company(connection, "greenhouse", "quiet", status="unverified")
        router.get(url__regex=self.BOARD["greenhouse"].format(slug="quiet")).mock(
            return_value=httpx.Response(200, json={"jobs": []}))
        assert self.probe(connection, config, http_client, clock).promoted == 1

    def test_unknown_slug_counts_a_failure_and_stays_unverified(self, connection, config, http_client, clock, router) -> None:
        company = insert_company(connection, "greenhouse", "ghost", status="unverified")
        router.get(url__regex=self.BOARD["greenhouse"].format(slug="ghost")).mock(
            return_value=httpx.Response(404, json={"status": 404}))
        summary = self.probe(connection, config, http_client, clock)
        row = connection.execute("SELECT * FROM companies WHERE id = ?", (company.id,)).fetchone()
        assert (summary.failed, row["status"], row["consecutive_failures"]) == (1, "unverified", 1)

    def test_repeated_failures_over_days_deactivate(self, connection, config, http_client, clock, router) -> None:
        company = insert_company(connection, "greenhouse", "ghost", status="unverified")
        router.get(url__regex=self.BOARD["greenhouse"].format(slug="ghost")).mock(return_value=httpx.Response(404, json={}))
        for _ in range(3):
            self.probe(connection, config, http_client, clock)
            clock.advance(days=1)
        assert connection.execute("SELECT status FROM companies WHERE id = ?", (company.id,)).fetchone()[0] == "inactive"

    def test_probe_respects_the_min_interval(self, connection, config, http_client, clock, router) -> None:
        insert_company(connection, "greenhouse", "ghost", status="unverified")
        route = router.get(url__regex=self.BOARD["greenhouse"].format(slug="ghost")).mock(return_value=httpx.Response(404, json={}))
        self.probe(connection, config, http_client, clock)
        clock.advance(hours=1)
        assert self.probe(connection, config, http_client, clock).probed == 0
        clock.advance(hours=12)
        assert self.probe(connection, config, http_client, clock).probed == 1
        assert route.call_count == 2

    def test_smartrecruiters_empty_response_does_not_promote(self, connection, config, http_client, clock, router) -> None:
        company = insert_company(connection, "smartrecruiters", "ghost", status="unverified")
        router.get(url__regex=self.BOARD["smartrecruiters"].format(slug="ghost")).mock(
            return_value=httpx.Response(200, json={"offset": 0, "limit": 100, "totalFound": 0, "content": []}))
        summary = self.probe(connection, config, http_client, clock)
        row = connection.execute("SELECT * FROM companies WHERE id = ?", (company.id,)).fetchone()
        assert (summary.promoted, summary.failed, row["status"]) == (0, 1, "unverified")

    def test_transient_errors_do_not_count_as_failures(self, connection, config, http_client, clock, router) -> None:
        company = insert_company(connection, "greenhouse", "flaky", status="unverified")
        router.get(url__regex=self.BOARD["greenhouse"].format(slug="flaky")).mock(return_value=httpx.Response(503))
        summary = self.probe(connection, config, http_client, clock)
        row = connection.execute("SELECT * FROM companies WHERE id = ?", (company.id,)).fetchone()
        assert (summary.transient, row["consecutive_failures"], row["status"]) == (1, 0, "unverified")

    def test_lever_probe_records_the_discovered_region(self, connection, config, http_client, clock, router) -> None:
        company = insert_company(connection, "lever", "eu-co", status="unverified")
        router.get(url__regex=r"https://api\.lever\.co/v0/postings/eu-co.*").mock(return_value=httpx.Response(404, json={}))
        router.get(url__regex=r"https://api\.eu\.lever\.co/v0/postings/eu-co.*").mock(
            return_value=httpx.Response(200, json=[{"id": "x", "text": "Engineer", "categories": {}}]))
        self.probe(connection, config, http_client, clock)
        row = connection.execute("SELECT * FROM companies WHERE id = ?", (company.id,)).fetchone()
        assert (row["status"], row["region"]) == ("active", "eu")

    def test_batches_one_bad_adapter_does_not_abort(self, connection, config, http_client, clock, router) -> None:
        insert_company(connection, "greenhouse", "weird", status="unverified")
        insert_company(connection, "greenhouse", "fine", status="unverified")
        router.get(url__regex=self.BOARD["greenhouse"].format(slug="weird")).mock(return_value=httpx.Response(200, json={"jobs": [{"id": 1, "title": "x"}] * 2, "meta": "not a dict"}))
        router.get(url__regex=self.BOARD["greenhouse"].format(slug="fine")).mock(return_value=httpx.Response(200, json={"jobs": []}))
        assert self.probe(connection, config, http_client, clock).promoted == 2


def test_full_discovery_pipeline_seed_then_probe(connection, config, http_client, clock, router, tmp_path: Path) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text("companies:\n  - {ats: greenhouse, slug: stripe, name: Stripe}\n  - {ats: ashby, slug: openai}\n", encoding="utf-8")
    router.get(url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/stripe/jobs.*").mock(
        return_value=httpx.Response(200, json={"jobs": [{"id": 1, "title": "Engineer"}]}))
    router.get(url__regex=r"https://api\.ashbyhq\.com/posting-api/job-board/openai.*").mock(return_value=httpx.Response(404))
    summary = run_discovery(connection, config, build_ats_sources(config), http_client, clock=clock, seed_path=seed,
                            use_common_crawl=False, use_openroles=False, use_snowball=False)
    assert summary.added == {"seed": 2}
    assert (summary.probe.promoted, summary.probe.failed) == (1, 1)
    statuses = {r["slug"]: r["status"] for r in connection.execute("SELECT slug, status FROM companies")}
    assert statuses == {"stripe": "active", "openai": "unverified"}
