"""Enterprise career sites: platform detection, robots/rate limits, the Workday adapter, dedupe, no-close."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import httpx
import pytest
import respx

from jobhunt.config import Config
from jobhunt.dedupe import upsert_jobs
from jobhunt.enterprise.detect import detect_all
from jobhunt.enterprise.polite import HostBlocked, PoliteFetcher
from jobhunt.enterprise.registry import detect_from_urls, detected_companies, parse_workday_url
from jobhunt.errors import TransientFetchError
from jobhunt.models import NormalizedJob
from jobhunt.sources.base import FeedContext
from jobhunt.sources.feeds.workday import WorkdaySource

HOST = "acme.wd5.myworkdayjobs.com"
LIST_URL = f"https://{HOST}/wday/cxs/acme/External/jobs"
COMPANY = {"name": "Acme", "params": {"tenant": "acme", "dc": "wd5", "host": HOST, "site": "External", "locale": "en-US"}}
NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


def fetcher(**kwargs: object) -> PoliteFetcher:
    return PoliteFetcher("jobhunt-test (contact@example.test)", sleep=lambda seconds: None, **kwargs)  # type: ignore[arg-type]


def posting(n: int, title: str = "Software Engineer") -> dict[str, object]:
    return {"title": f"{title} {n}", "externalPath": f"/job/Pune/{title.replace(' ', '-')}-{n}_R-{n}",
            "locationsText": "2 Locations", "postedOn": "Posted Today", "bulletFields": [f"R-{n}"]}


def detail(n: int) -> dict[str, object]:
    return {"jobPostingInfo": {"jobDescription": "<p>Build <b>APIs</b> in Python.</p>", "location": "Pune, India",
                               "additionalLocations": ["Remote, India"], "startDate": "2026-10-12",
                               "timeType": "Full time", "remoteType": "Hybrid", "jobReqId": f"R-{n}"}}


def robots_ok(router: respx.MockRouter, host: str = HOST, body: str = "User-agent: *\nDisallow: /search\n") -> None:
    router.get(f"https://{host}/robots.txt").mock(return_value=httpx.Response(200, text=body))


def source(**options: object) -> WorkdaySource:
    base = {"companies": [COMPANY], "queries": ["software engineer"], "per_company_cap": 400, "max_pages_per_query": 5}
    base.update(options)
    return WorkdaySource(base, fetcher=fetcher())


def context(keep=lambda title: True) -> FeedContext:  # noqa: ANN001
    return FeedContext(now=NOW, keep_title=keep)


class _Http:
    user_agent = "jobhunt-test"


# --- URL parsing and detection -------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("https://mastercard.wd1.myworkdayjobs.com/CorporateCareers", ("mastercard", "wd1", "CorporateCareers", "en-US")),
    ("https://citi.wd5.myworkdayjobs.com/en-US/2/userHome", ("citi", "wd5", "2", "en-US")),
    ("https://x.wd103.myworkdayjobs.com/de-DE/Ext/job/Berlin/Dev_R1", ("x", "wd103", "Ext", "de-DE")),
])
def test_workday_urls_are_parsed(url, expected):
    parsed = parse_workday_url(url)
    assert parsed and (parsed["tenant"], parsed["dc"], parsed["site"], parsed["locale"]) == expected


@pytest.mark.parametrize("url", ["https://example.com/en-US/x", "https://acme.wd5.myworkdayjobs.com/", "not a url"])
def test_non_workday_urls_are_ignored(url):
    assert parse_workday_url(url) is None


def test_detection_finds_platforms_in_urls_and_page_text():
    assert detect_from_urls(["https://x.com/careers", "https://acme.wd5.myworkdayjobs.com/en-US/External"]).platform == "workday"
    page = '<a href="https://boards.greenhouse.io/acme/jobs/1">Apply</a>'
    found = detect_from_urls(["https://acme.com/careers"], page)
    assert (found.platform, found.params["slug"]) == ("greenhouse", "acme")
    assert detect_from_urls(["https://a.com"], "<script src='https://x.eightfold.ai/a.js'>").platform == "eightfold"
    assert detect_from_urls(["https://a.com"], "<p>hello</p>").platform == "unknown"
    oracle = detect_from_urls(["https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX"])
    assert oracle.platform == "oracle"


def test_detect_all_stores_evidence_and_adds_supported_boards(connection: sqlite3.Connection, router: respx.MockRouter) -> None:
    connection.executemany(
        "INSERT INTO enterprise_companies (name, careers_url, created_at) VALUES (?, ?, '2026-10-14T00:00:00Z')",
        [("Acme", "https://acme.example/careers"), ("Globex", "https://globex.example/jobs"),
         ("Initech", "https://initech.example/careers"), ("Hooli", "https://hooli.example/careers")],
    )
    for host in ("acme.example", "globex.example", "initech.example", "hooli.example"):
        router.get(f"https://{host}/robots.txt").mock(return_value=httpx.Response(404))
    router.get("https://acme.example/careers").mock(return_value=httpx.Response(
        302, headers={"location": f"https://{HOST}/en-US/External"}))
    router.get(f"https://{HOST}/robots.txt").mock(return_value=httpx.Response(404))
    router.get(f"https://{HOST}/en-US/External").mock(return_value=httpx.Response(200, html="<html>jobs</html>"))
    router.get("https://globex.example/jobs").mock(return_value=httpx.Response(
        200, html='<a href="https://jobs.lever.co/globex/abc-123">x</a>'))
    router.get("https://initech.example/careers").mock(return_value=httpx.Response(200, html="<html>bespoke</html>"))
    router.get("https://hooli.example/careers").mock(return_value=httpx.Response(404))

    summary = detect_all(connection, fetcher())
    rows = {r["name"]: r for r in connection.execute("SELECT * FROM enterprise_companies")}
    assert (rows["Acme"]["detected_ats"], rows["Acme"]["status"]) == ("workday", "detected")
    assert json.loads(rows["Acme"]["adapter_params"])["site"] == "External"
    assert rows["Globex"]["detected_ats"] == "lever" and rows["Globex"]["status"] == "detected"
    assert rows["Initech"]["status"] == "needs_adapter" and rows["Initech"]["detected_ats"] == "unknown"
    assert rows["Hooli"]["status"] == "unreachable"
    assert summary.added_to_companies == 1
    assert connection.execute("SELECT slug FROM companies WHERE ats_type = 'lever'").fetchone()["slug"] == "globex"
    assert [c["name"] for c in detected_companies(connection, "workday")] == ["Acme"]


# --- robots, rate limit, bot protection ---------------------------------------------------------------


def test_robots_disallow_is_obeyed(router: respx.MockRouter) -> None:
    robots_ok(router, "blocked.example", "User-agent: *\nDisallow: /wday/\n")
    route = router.post("https://blocked.example/wday/cxs/a/b/jobs").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(HostBlocked):
        fetcher().post_json("https://blocked.example/wday/cxs/a/b/jobs", {})
    assert not route.called


def test_robots_server_error_blocks_the_host_and_is_cached(router: respx.MockRouter) -> None:
    router.get("https://down.example/robots.txt").mock(return_value=httpx.Response(503))
    fetch = fetcher()
    for _ in range(2):
        with pytest.raises(HostBlocked):
            fetch.get("https://down.example/careers")
    assert router.calls.call_count == 1


def test_requests_are_spaced_per_host(router: respx.MockRouter) -> None:
    robots_ok(router, "slow.example", "")
    router.get("https://slow.example/a").mock(return_value=httpx.Response(200, text="ok"))
    sleeps: list[float] = []
    clock = [0.0]
    fetch = PoliteFetcher("ua", requests_per_minute=20, sleep=sleeps.append, monotonic=lambda: clock[0])
    for _ in range(3):
        fetch.get("https://slow.example/a")
    assert sum(1 for s in sleeps if abs(s - 3.0) < 0.01 or s > 2.9) >= 2        # 20/min = one request per 3 s


def test_captcha_page_blocks_the_host(router: respx.MockRouter) -> None:
    robots_ok(router, "guarded.example", "")
    router.get("https://guarded.example/careers").mock(return_value=httpx.Response(
        200, html="<html><title>Just a moment...</title>Please complete the CAPTCHA</html>"))
    fetch = fetcher()
    with pytest.raises(HostBlocked):
        fetch.get("https://guarded.example/careers")
    assert "guarded.example" in fetch.blocked


def test_persistent_429_blocks_the_host(router: respx.MockRouter) -> None:
    robots_ok(router, "busy.example", "")
    router.get("https://busy.example/x").mock(return_value=httpx.Response(429))
    fetch = fetcher(max_retries=1)
    with pytest.raises(HostBlocked):
        fetch.get("https://busy.example/x")


# --- Workday adapter -----------------------------------------------------------------------------------


def test_pagination_prefilter_before_detail_and_normalization(router: respx.MockRouter) -> None:
    robots_ok(router)
    pages = [
        httpx.Response(200, json={"total": 25, "jobPostings": [posting(i) for i in range(20)]}),
        httpx.Response(200, json={"jobPostings": [posting(i) for i in range(20, 24)] + [posting(99, "Sales Manager")]}),
    ]
    list_route = router.post(LIST_URL).mock(side_effect=pages)
    detail_routes = {}
    for n in list(range(24)):
        path = f"/job/Pune/Software-Engineer-{n}_R-{n}"
        detail_routes[n] = router.get(f"https://{HOST}/wday/cxs/acme/External{path}").mock(
            return_value=httpx.Response(200, json=detail(n)))
    sales = router.get(f"https://{HOST}/wday/cxs/acme/External/job/Pune/Sales-Manager-99_R-99").mock(
        return_value=httpx.Response(200, json=detail(99)))

    result = source().fetch(_Http(), context(keep=lambda title: "Sales" not in title))   # type: ignore[arg-type]
    assert list_route.call_count == 2 and len(result.jobs) == 24
    assert not sales.called                                  # filtered by title: no detail request
    job = result.jobs[0]
    assert job.source == "workday" and job.company_name == "Acme"
    assert job.apply_url == f"https://{HOST}/en-US/External/job/Pune/Software-Engineer-0_R-0"
    assert job.location_raw == "Pune, India; Remote, India" and job.posted_at == "2026-10-12T00:00:00Z"
    assert job.description_text == "Build APIs in Python." and job.is_remote is False
    body = json.loads(list_route.calls[0].request.content)
    assert body == {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "software engineer"}


def test_per_company_cap_stops_paging(router: respx.MockRouter) -> None:
    robots_ok(router)
    route = router.post(LIST_URL).mock(side_effect=lambda request: httpx.Response(200, json={
        "jobPostings": [posting(json.loads(request.content)["offset"] + i) for i in range(20)]}))
    router.get(url__regex=rf"https://{HOST}/wday/cxs/acme/External/job/.*").mock(
        return_value=httpx.Response(200, json=detail(1)))
    result = source(per_company_cap=40, detail_cap_per_company=0).fetch(_Http(), context())  # type: ignore[arg-type]
    assert len(result.jobs) == 40 and route.call_count == 2


def test_india_facet_variant_of_the_query_is_added(router: respx.MockRouter) -> None:
    robots_ok(router)
    facets = [{"facetParameter": "locationCountry", "values": [{"descriptor": "India", "id": "abc"}]}]

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["appliedFacets"]:
            return httpx.Response(200, json={"jobPostings": [posting(7, "India Engineer")]})
        return httpx.Response(200, json={"facets": facets, "jobPostings": [posting(1)]})

    router.post(LIST_URL).mock(side_effect=answer)
    router.get(url__regex=rf"https://{HOST}/wday/cxs/.*").mock(return_value=httpx.Response(200, json=detail(1)))
    result = source().fetch(_Http(), context())          # type: ignore[arg-type]
    assert {j.title for j in result.jobs} == {"Software Engineer 1", "India Engineer 7"}


def test_empty_board_is_not_an_error(router: respx.MockRouter) -> None:
    robots_ok(router)
    router.post(LIST_URL).mock(return_value=httpx.Response(200, json={"total": 0, "jobPostings": []}))
    assert source().fetch(_Http(), context()).jobs == []  # type: ignore[arg-type]


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "https://acme.wd5.myworkdayjobs.com/maintenance"}),
    httpx.Response(200, text="<html>maintenance</html>", headers={"content-type": "text/html"}),
    httpx.Response(200, text="{not json"),
    httpx.Response(404),
    httpx.Response(200, json=["unexpected"]),
])
def test_bad_answers_fail_the_run_without_returning_jobs(router: respx.MockRouter, response: httpx.Response) -> None:
    robots_ok(router)
    router.post(LIST_URL).mock(return_value=response)
    with pytest.raises(TransientFetchError):
        source().fetch(_Http(), context())               # type: ignore[arg-type]


def test_one_bad_company_does_not_lose_the_others(router: respx.MockRouter) -> None:
    robots_ok(router)
    robots_ok(router, "bad.wd1.myworkdayjobs.com")
    router.post("https://bad.wd1.myworkdayjobs.com/wday/cxs/bad/Site/jobs").mock(return_value=httpx.Response(404))
    router.post(LIST_URL).mock(return_value=httpx.Response(200, json={"jobPostings": [posting(1)]}))
    router.get(url__regex=rf"https://{HOST}/wday/cxs/.*").mock(return_value=httpx.Response(200, json=detail(1)))
    bad = {"name": "Bad", "params": {"tenant": "bad", "dc": "wd1", "host": "bad.wd1.myworkdayjobs.com", "site": "Site"}}
    result = source(companies=[bad, COMPANY]).fetch(_Http(), context())    # type: ignore[arg-type]
    assert len(result.jobs) == 1 and any("Bad" in w for w in result.warnings)


def test_429_blocks_that_host_only(router: respx.MockRouter) -> None:
    robots_ok(router)
    router.post(LIST_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(TransientFetchError) as caught:
        source().fetch(_Http(), context())               # type: ignore[arg-type]
    assert "429" in str(caught.value)


def test_no_companies_returns_an_explained_empty_result() -> None:
    result = WorkdaySource({"companies": []}).fetch(_Http(), context())    # type: ignore[arg-type]
    assert result.jobs == [] and "detect-ats" in result.warnings[0]


# --- pipeline behaviour -------------------------------------------------------------------------------


def _job(source_name: str, source_id: str) -> NormalizedJob:
    return NormalizedJob(source=source_name, source_job_id=source_id, title="Software Engineer II", company_name="Acme",
                         apply_url=f"https://example.test/{source_id}", location_raw="Pune, India", country="IN",
                         city="Pune", description_text="Build things.")


def test_same_job_from_workday_and_another_source_merges(connection: sqlite3.Connection) -> None:
    seen = "2026-10-14T00:00:00Z"
    first = upsert_jobs(connection, [_job("workday", "acme:/job/1")], company_id=None, seen_at=seen)
    second = upsert_jobs(connection, [_job("email_linkedin", "li-77")], company_id=None, seen_at=seen)
    assert first.created == 1 and second.merged == 1
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    sources = {r[0] for r in connection.execute("SELECT source FROM job_sightings")}
    assert sources == {"workday", "email_linkedin"}


def test_a_failed_workday_run_never_closes_existing_jobs(connection: sqlite3.Connection, config: Config,
                                                         router: respx.MockRouter) -> None:
    from jobhunt.http import HttpClient
    from jobhunt.runner import IngestionRunner

    upsert_jobs(connection, [_job("workday", "acme:/job/1")], company_id=None, seen_at="2026-10-01T00:00:00Z")
    robots_ok(router)
    router.post(LIST_URL).mock(return_value=httpx.Response(503))
    feed = WorkdaySource({"companies": [COMPANY], "queries": ["x"]}, fetcher=fetcher(max_retries=0))
    with HttpClient(config.http, "ua", sleep=lambda s: None) as http:
        summaries = IngestionRunner(connection, config, {"workday": feed}, http).run(["workday"], force=True)
    assert summaries[0].jobs_closed == 0 and summaries[0].errors
    assert connection.execute("SELECT status FROM jobs").fetchone()["status"] == "open"


# --- amazon.jobs ------------------------------------------------------------------------------------------

AMAZON = "https://www.amazon.jobs/en/search.json"


def amazon_item(n: int, title: str = "Software Development Engineer") -> dict[str, object]:
    return {"id_icims": str(1000 + n), "title": f"{title} {n}", "job_path": f"/en/jobs/{1000 + n}/sde",
            "description": "<p>Build services.</p>", "basic_qualifications": "- Python", "normalized_location":
            "Bengaluru, Karnataka, IND", "city": "Bengaluru", "country_code": "IND", "posted_date": "October 13, 2026",
            "company_name": "Amazon Dev Center India", "job_schedule_type": "full-time", "job_category": "SDE",
            "url_next_step": f"https://account.amazon.com/jobs/{1000 + n}/apply"}


def amazon_source(**options: object):  # noqa: ANN201
    from jobhunt.sources.feeds.amazon_jobs import AmazonJobsSource
    return AmazonJobsSource({"queries": ["software engineer"], **options}, fetcher=fetcher())


def test_amazon_jobs_filters_to_india_and_normalizes(router: respx.MockRouter) -> None:
    robots_ok(router, "www.amazon.jobs", "User-agent: *\nDisallow: /internal\n")
    route = router.get(AMAZON).mock(return_value=httpx.Response(200, json={"jobs": [amazon_item(1), amazon_item(2)]}))
    result = amazon_source().fetch(_Http(), context())          # type: ignore[arg-type]
    assert [j.source_job_id for j in result.jobs] == ["1001", "1002"]
    request = route.calls[0].request
    assert "normalized_country_code%5B%5D=IND" in str(request.url) and "result_limit=100" in str(request.url)
    job = result.jobs[0]
    assert job.apply_url.startswith("https://account.amazon.com/") and job.posted_at == "2026-10-13T00:00:00Z"
    assert "Build services." in (job.description_text or "") and job.location_raw == "Bengaluru, Karnataka, IND"


def test_amazon_jobs_respects_robots_and_pagination_cap(router: respx.MockRouter) -> None:
    robots_ok(router, "www.amazon.jobs", "User-agent: *\nDisallow: /en/search.json\n")
    route = router.get(AMAZON).mock(return_value=httpx.Response(200, json={"jobs": []}))
    with pytest.raises(TransientFetchError):
        amazon_source().fetch(_Http(), context())               # type: ignore[arg-type]
    assert not route.called


def test_amazon_jobs_pages_until_cap(router: respx.MockRouter) -> None:
    robots_ok(router, "www.amazon.jobs", "")
    route = router.get(AMAZON).mock(side_effect=lambda request: httpx.Response(200, json={
        "jobs": [amazon_item(int(request.url.params["offset"]) + i) for i in range(100)]}))
    result = amazon_source(per_run_cap=150).fetch(_Http(), context())    # type: ignore[arg-type]
    assert len(result.jobs) == 150 and route.call_count == 2


def test_amazon_jobs_malformed_json_is_a_failure(router: respx.MockRouter) -> None:
    robots_ok(router, "www.amazon.jobs", "")
    router.get(AMAZON).mock(return_value=httpx.Response(200, text="<html>nope</html>",
                                                          headers={"content-type": "application/json"}))
    with pytest.raises(TransientFetchError):
        amazon_source().fetch(_Http(), context())               # type: ignore[arg-type]
