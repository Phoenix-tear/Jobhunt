"""Eightfold / Phenom / Oracle Recruiting Cloud adapters (mocked): pagination, prefilter, empty, 429, malformed."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from jobhunt.enterprise.detect import _enrich
from jobhunt.enterprise.polite import PoliteFetcher
from jobhunt.enterprise.registry import Detection
from jobhunt.errors import TransientFetchError
from jobhunt.sources.base import FeedContext
from jobhunt.sources.feeds.enterprise_boards import EightfoldSource, OracleSource, PhenomSource

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


class _Http:
    user_agent = "jobhunt-test"


def fetcher(**kwargs: object) -> PoliteFetcher:
    return PoliteFetcher("jobhunt-test (contact@example.test)", sleep=lambda s: None, **kwargs)  # type: ignore[arg-type]


def ctx(keep=lambda title: True) -> FeedContext:  # noqa: ANN001
    return FeedContext(now=NOW, keep_title=keep)


def robots_ok(router: respx.MockRouter, host: str) -> None:
    router.get(f"https://{host}/robots.txt").mock(return_value=httpx.Response(404))


# --- Eightfold ----------------------------------------------------------------------------------------

EF_HOST = "acme.eightfold.ai"
EF = {"name": "Acme", "params": {"host": EF_HOST, "domain": "acme.com"}}


def ef_position(n: int, title: str = "Software Engineer") -> dict:
    return {"id": 1000 + n, "displayJobId": f"R{n}", "name": f"{title} {n}", "locations": ["Bengaluru, India"],
            "postedTs": 1791849600, "workLocationOption": "hybrid", "positionUrl": f"/careers/job/{1000 + n}"}


def ef_source(**options: object) -> EightfoldSource:
    return EightfoldSource({"companies": [EF], "queries": ["software engineer"], "locations": ["India"],
                            "page_size": 10, **options}, fetcher=fetcher())


def test_eightfold_pages_prefilters_and_fetches_details(router: respx.MockRouter) -> None:
    robots_ok(router, EF_HOST)
    router.get(f"https://{EF_HOST}/careers").mock(return_value=httpx.Response(200, text="<html>careers</html>"))
    answers = [{"data": {"positions": [ef_position(i) for i in range(10)]}},
               {"data": {"positions": [ef_position(10), ef_position(11, "Sales Manager")]}}]
    search = router.get(f"https://{EF_HOST}/api/pcsx/search").mock(
        side_effect=[httpx.Response(200, json=a) for a in answers])
    details = router.get(f"https://{EF_HOST}/api/pcsx/position_details").mock(return_value=httpx.Response(
        200, json={"data": {"jobDescription": "<p>Build <b>APIs</b>.</p>"}}))
    result = ef_source().fetch(_Http(), ctx(keep=lambda title: "Sales" not in title))   # type: ignore[arg-type]
    assert search.call_count == 2 and len(result.jobs) == 11
    assert details.call_count == 11                       # the filtered title got no detail request
    job = result.jobs[0]
    assert job.source == "eightfold" and job.apply_url == f"https://{EF_HOST}/careers/job/1000"
    assert job.description_text == "Build APIs." and job.is_remote is False and job.location_raw == "Bengaluru, India"


def test_eightfold_403_without_the_page_cookie_is_a_failure(router: respx.MockRouter) -> None:
    robots_ok(router, EF_HOST)
    router.get(f"https://{EF_HOST}/careers").mock(return_value=httpx.Response(200, text="ok"))
    router.get(f"https://{EF_HOST}/api/pcsx/search").mock(return_value=httpx.Response(
        403, json={"message": "Not authorized for PCSX"}))
    with pytest.raises(TransientFetchError):
        ef_source().fetch(_Http(), ctx())                  # type: ignore[arg-type]


# --- Phenom ---------------------------------------------------------------------------------------------

PH_HOST = "careers.acme.com"
PH = {"name": "Acme", "params": {"host": PH_HOST, "locale": "us/en"}}


def ph_job(n: int, title: str = "Software Engineer") -> dict:
    return {"jobId": f"R{n}", "title": f"{title} {n}", "location": "Noida, Uttar Pradesh, India",
            "descriptionTeaser": "Build things.", "ml_skills": ["python", "aws"], "type": "Full time",
            "postedDate": "2026-10-13T00:00:00.000+0000", "applyUrl": f"https://acme.wd5.myworkdayjobs.com/job/{n}"}


def ph_source(**options: object) -> PhenomSource:
    return PhenomSource({"companies": [PH], "queries": ["software engineer"], "page_size": 10, **options},
                        fetcher=fetcher())


def ph_page(router: respx.MockRouter) -> None:
    robots_ok(router, PH_HOST)
    router.get(f"https://{PH_HOST}/us/en/search-results").mock(return_value=httpx.Response(
        200, text='<script>phApp.ddo={"refNum":"ACMEUS","pageId":"page15"}</script>'))


def test_phenom_posts_widgets_with_refnum_and_pages(router: respx.MockRouter) -> None:
    ph_page(router)
    route = router.post(f"https://{PH_HOST}/widgets").mock(side_effect=lambda request: httpx.Response(200, json={
        "refineSearch": {"data": {"jobs": [ph_job(json.loads(request.content)["from"] + i) for i in range(10)]
                                  if json.loads(request.content)["from"] < 10 else [ph_job(99, "Sales Manager")]}}}))
    result = ph_source().fetch(_Http(), ctx(keep=lambda title: "Sales" not in title))   # type: ignore[arg-type]
    first = json.loads(route.calls[0].request.content)
    assert first["refNum"] == "ACMEUS" and first["keywords"] == "software engineer"
    assert first["selected_fields"] == {"country": ["India"]}          # India first
    assert len(result.jobs) == 10
    job = result.jobs[0]
    assert job.source == "phenom" and "Skills: python, aws" in job.description_text
    assert job.posted_at == "2026-10-13T00:00:00Z"


def test_phenom_missing_refnum_is_a_clean_failure(router: respx.MockRouter) -> None:
    robots_ok(router, PH_HOST)
    router.get(f"https://{PH_HOST}/us/en/search-results").mock(return_value=httpx.Response(200, text="<html></html>"))
    with pytest.raises(TransientFetchError):
        ph_source().fetch(_Http(), ctx())                  # type: ignore[arg-type]


# --- Oracle Recruiting Cloud -------------------------------------------------------------------------------

OR_HOST = "acme.fa.us2.oraclecloud.com"
OR = {"name": "Acme", "params": {"host": OR_HOST, "site": "CX_1001"}}
OR_URL = f"https://{OR_HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"


def or_req(n: int, title: str = "Software Engineer") -> dict:
    return {"Id": str(n), "Title": f"{title} {n}", "PostedDate": "2026-10-13", "PrimaryLocation": "Pune, India",
            "PrimaryLocationCountry": "IN", "WorkplaceType": "Hybrid", "ShortDescriptionStr": "Build."}


def or_source(**options: object) -> OracleSource:
    return OracleSource({"companies": [OR], "queries": ["software engineer"], "page_size": 5, **options},
                        fetcher=fetcher())


def test_oracle_lists_requisitions_anonymously_with_the_site_number(router: respx.MockRouter) -> None:
    robots_ok(router, OR_HOST)
    pages = [{"items": [{"requisitionList": [or_req(i) for i in range(5)]}]},
             {"items": [{"requisitionList": [or_req(5), or_req(6, "Account Manager")]}]}]
    route = router.get(OR_URL).mock(side_effect=[httpx.Response(200, json=p) for p in pages])
    result = or_source().fetch(_Http(), ctx(keep=lambda title: "Manager" not in title))   # type: ignore[arg-type]
    assert route.call_count == 2 and len(result.jobs) == 6
    from urllib.parse import unquote
    assert "siteNumber=CX_1001" in unquote(str(route.calls[0].request.url))
    assert "Authorization" not in route.calls[0].request.headers
    job = result.jobs[0]
    assert job.apply_url == f"https://{OR_HOST}/hcmUI/CandidateExperience/en/sites/CX_1001/job/0"
    assert job.country == "IN" and job.is_remote is False


def test_oracle_host_with_a_blocked_robots_txt_is_skipped(router: respx.MockRouter) -> None:
    router.get(f"https://{OR_HOST}/robots.txt").mock(return_value=httpx.Response(403, text="W4S-402 Blocked by WAF"))
    route = router.get(OR_URL).mock(return_value=httpx.Response(200, json={"items": []}))
    with pytest.raises(TransientFetchError):
        or_source().fetch(_Http(), ctx())                  # type: ignore[arg-type]
    assert not route.called


# --- failures shared by all three -----------------------------------------------------------------------------


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "https://x.example/maintenance"}),
    httpx.Response(200, text="<html>maintenance</html>"),
    httpx.Response(200, json=["unexpected"]),
    httpx.Response(429),
])
def test_bad_answers_never_return_jobs(router: respx.MockRouter, response: httpx.Response) -> None:
    robots_ok(router, OR_HOST)
    router.get(OR_URL).mock(return_value=response)
    with pytest.raises(TransientFetchError):
        OracleSource({"companies": [OR], "queries": ["x"]}, fetcher=fetcher(max_retries=0)).fetch(
            _Http(), ctx())                                # type: ignore[arg-type]


def test_empty_boards_are_not_errors(router: respx.MockRouter) -> None:
    robots_ok(router, OR_HOST)
    router.get(OR_URL).mock(return_value=httpx.Response(200, json={"items": [{"requisitionList": []}]}))
    assert or_source().fetch(_Http(), ctx()).jobs == []     # type: ignore[arg-type]


# --- detect-ats enrichment (HTML fixtures) --------------------------------------------------------------------------


def test_detection_enrichment_finds_each_platforms_parameters(router: respx.MockRouter) -> None:
    robots_ok(router, "acme.eightfold.ai")
    router.get("https://acme.eightfold.ai/careers").mock(return_value=httpx.Response(
        200, text='<script>{"domain":"acme.com"}</script>'))
    found = _enrich(fetcher(), Detection("eightfold", {}, "marker"), ["https://www.acme.com/careers"],
                    '<iframe src="https://acme.eightfold.ai/careers?x=1"></iframe>')
    assert found.params == {"host": "acme.eightfold.ai", "domain": "acme.com"}

    oracle = _enrich(fetcher(), Detection("oracle", {}, "marker"), ["https://acme.com/jobs"],
                     '<a href="https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/jobs">')
    assert oracle.params == {"host": "eeho.fa.us2.oraclecloud.com", "site": "CX_45001"}

    robots_ok(router, "careers.acme.com")
    router.get("https://careers.acme.com/us/en/search-results").mock(return_value=httpx.Response(
        200, text='{"refNum":"ACMEUS"}'))
    phenom = _enrich(fetcher(), Detection("phenom", {}, "marker"), ["https://careers.acme.com/us/en/home"], "")
    assert phenom.params == {"host": "careers.acme.com", "locale": "us/en"}
