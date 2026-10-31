"""Behaviors every ATS adapter must share: payload edge cases and every failure mode."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx

from jobhunt.errors import (
    AuthRequiredError, InvalidResponseError, NotFoundError, TransientFetchError,
)
from jobhunt.http import HttpClient
from jobhunt.models import CompanyRef, FetchResult
from jobhunt.sources.ashby import AshbySource
from jobhunt.sources.base import FetchContext, JobSource
from jobhunt.sources.greenhouse import GreenhouseSource
from jobhunt.sources.lever import LeverSource
from jobhunt.sources.recruitee import RecruiteeSource
from jobhunt.sources.smartrecruiters import SmartRecruitersSource
from jobhunt.sources.workable import WorkableSource

NO_DETAIL = FetchContext(wants_detail=lambda job: False)
ITEM_DESCRIPTION_HTML = "<p>Build <b>great</b> things &amp; ship them.</p><ul><li>Python</li></ul>"


@dataclass(frozen=True)
class AdapterCase:
    name: str
    source_class: type[JobSource]
    slug: str
    url_regex: str
    item: Callable[[str, str], dict[str, Any]]          # (job id, title) -> a complete raw job item
    minimal_item: Callable[[str], dict[str, Any]]       # only required fields; optionals explicitly null
    wrap: Callable[[list[Any]], Any]                    # items -> whole payload
    not_a_board: Any                                    # payload of the wrong shape
    html_in_description: bool = True

    def company(self) -> CompanyRef:
        return CompanyRef(id=1, name="Acme", ats_type=self.name, slug=self.slug, status="active")


def _greenhouse_item(job_id: str, title: str) -> dict[str, Any]:
    return {
        "id": int(job_id), "title": title, "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "location": {"name": "Bengaluru, India"}, "updated_at": "2026-10-01T10:00:00-04:00",
        "first_published": "2026-09-01T10:00:00-04:00", "company_name": "Acme",
        "content": "&lt;p&gt;Build &lt;b&gt;great&lt;/b&gt; things &amp;amp; ship them.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;",
    }


def _lever_item(job_id: str, title: str) -> dict[str, Any]:
    return {
        "id": job_id, "text": title, "hostedUrl": f"https://jobs.lever.co/acme/{job_id}",
        "applyUrl": f"https://jobs.lever.co/acme/{job_id}/apply", "createdAt": 1788199939703,
        "categories": {"location": "Pune, India", "commitment": "Full-time", "allLocations": ["Pune, India"]},
        "descriptionPlain": "Build great things & ship them.\n- Python", "lists": [], "workplaceType": "hybrid",
    }


def _ashby_item(job_id: str, title: str) -> dict[str, Any]:
    return {
        "id": job_id, "title": title, "location": "Hyderabad, India", "isListed": True, "isRemote": False,
        "workplaceType": "OnSite", "employmentType": "FullTime", "publishedAt": "2026-10-01T10:00:00.000+00:00",
        "jobUrl": f"https://jobs.ashbyhq.com/acme/{job_id}", "applyUrl": f"https://jobs.ashbyhq.com/acme/{job_id}/application",
        "descriptionHtml": ITEM_DESCRIPTION_HTML, "secondaryLocations": [],
    }


def _workable_item(job_id: str, title: str) -> dict[str, Any]:
    return {
        "shortcode": job_id, "title": title, "url": f"https://apply.workable.com/j/{job_id}",
        "description": ITEM_DESCRIPTION_HTML, "published_on": "2026-10-01", "employment_type": "Full-time",
        "telecommuting": False, "locations": [{"city": "Pune", "region": "MH", "country": "India", "countryCode": "IN", "hidden": False}],
    }


def _smartrecruiters_item(job_id: str, title: str) -> dict[str, Any]:
    return {
        "id": job_id, "name": title, "company": {"identifier": "acme", "name": "Acme"},
        "releasedDate": "2026-10-01T10:00:00.000Z", "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
        "experienceLevel": {"id": "entry_level", "label": "Entry Level"},
        "location": {"city": "Pune", "country": "in", "remote": False, "hybrid": False, "fullLocation": "Pune, MH, India"},
        "ref": f"https://api.smartrecruiters.com/v1/companies/acme/postings/{job_id}",
    }


def _recruitee_item(job_id: str, title: str) -> dict[str, Any]:
    return {
        "id": int(job_id), "title": title, "slug": f"job-{job_id}", "status": "published", "company_name": "Acme",
        "careers_url": f"https://acme.recruitee.com/o/job-{job_id}", "location": "Pune, India",
        "city": "Pune", "country_code": "IN", "remote": False, "hybrid": False, "on_site": True,
        "employment_type_code": "fulltime_permanent", "published_at": "2026-10-05 16:05:18 UTC",
        "description": ITEM_DESCRIPTION_HTML, "requirements": "<p>Know Python.</p>",
        "salary": {"min": None, "max": None, "period": None, "currency": None},
    }


CASES = [
    AdapterCase(
        "greenhouse", GreenhouseSource, "acme", r"https://boards-api\.greenhouse\.io/v1/boards/acme/jobs.*",
        _greenhouse_item, lambda i: {"id": int(i), "title": f"Engineer {i}", "location": None, "content": None, "first_published": None},
        lambda items: {"jobs": items, "meta": {"total": len(items)}}, {"error": "x"},
    ),
    AdapterCase(
        "lever", LeverSource, "acme", r"https://api\.(eu\.)?lever\.co/v0/postings/acme.*",
        _lever_item, lambda i: {"id": i, "text": f"Engineer {i}", "categories": None, "descriptionPlain": None, "createdAt": None},
        lambda items: items, {"error": "not a list"},
    ),
    AdapterCase(
        "ashby", AshbySource, "acme", r"https://api\.ashbyhq\.com/posting-api/job-board/acme.*",
        _ashby_item, lambda i: {"id": i, "title": f"Engineer {i}", "location": None, "publishedAt": None, "descriptionHtml": None},
        lambda items: {"jobs": items, "apiVersion": "1"}, {"apiVersion": "1"},
    ),
    AdapterCase(
        "workable", WorkableSource, "acme", r"https://apply\.workable\.com/api/v1/widget/accounts/acme.*",
        _workable_item, lambda i: {"shortcode": i, "title": f"Engineer {i}", "locations": None, "description": None},
        lambda items: {"name": "Acme Inc", "jobs": items}, {"name": "Acme Inc"},
    ),
    AdapterCase(
        "smartrecruiters", SmartRecruitersSource, "acme", r"https://api\.smartrecruiters\.com/v1/companies/acme/postings.*",
        _smartrecruiters_item, lambda i: {"id": i, "name": f"Engineer {i}", "location": None, "company": None},
        lambda items: {"offset": 0, "limit": 100, "totalFound": len(items), "content": items}, {"totalFound": 0},
        html_in_description=False,  # list endpoint has no description; covered in its own tests
    ),
    AdapterCase(
        "recruitee", RecruiteeSource, "acme", r"https://acme\.recruitee\.com/api/offers/.*",
        _recruitee_item, lambda i: {"id": int(i), "title": f"Engineer {i}", "location": None, "description": None, "salary": None},
        lambda items: {"offers": items}, {"offers": "nope"},
    ),
]
CASE_IDS = [case.name for case in CASES]


@pytest.fixture(params=CASES, ids=CASE_IDS)
def case(request: pytest.FixtureRequest) -> AdapterCase:
    return request.param


def run(case: AdapterCase, http: HttpClient, context: FetchContext = NO_DETAIL) -> FetchResult:
    return case.source_class().fetch_company(case.company(), http, context)


def board(case: AdapterCase, router: respx.MockRouter) -> respx.Route:
    return router.get(url__regex=case.url_regex)


def test_normal_payload(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    board(case, router).mock(return_value=httpx.Response(200, json=case.wrap([case.item("101", "Backend Engineer"), case.item("102", "Data Engineer")])))
    result = run(case, http_client)
    assert [job.source_job_id for job in result.jobs] == ["101", "102"]
    first = result.jobs[0]
    assert first.source == case.name
    assert first.title == "Backend Engineer"
    assert first.apply_url and first.apply_url.startswith("https://")
    assert first.posted_at is None or first.posted_at.endswith("Z")
    assert first.location_raw
    if case.html_in_description:
        assert first.description_text and "<" not in first.description_text and "&amp;" not in first.description_text
        assert first.needs_description is False
        assert "Python" in first.description_text
    else:
        assert first.needs_description is True


def test_empty_board_is_a_valid_result(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    board(case, router).mock(return_value=httpx.Response(200, json=case.wrap([])))
    result = run(case, http_client)
    assert result.jobs == [] and result.seen_source_job_ids == set()


def test_missing_and_null_fields_never_crash(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    items = [None, {}, {"unexpected": [1, 2]}, "string item", 7, case.minimal_item("201"), case.item("202", "Real Job")]
    board(case, router).mock(return_value=httpx.Response(200, json=case.wrap(items)))
    result = run(case, http_client)
    assert [job.source_job_id for job in result.jobs] == ["201", "202"]
    assert result.malformed_items >= 4
    minimal = result.jobs[0]
    assert minimal.title == "Engineer 201"
    assert minimal.posted_at is None
    assert minimal.needs_description is True or case.name == "smartrecruiters"


def test_items_missing_a_title_are_skipped_not_fatal(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    broken = case.item("301", "x")
    for key in ("title", "text", "name"):
        broken.pop(key, None)
    board(case, router).mock(return_value=httpx.Response(200, json=case.wrap([broken, case.item("302", "Ok")])))
    assert [job.source_job_id for job in run(case, http_client).jobs] == ["302"]


def test_duplicate_ids_inside_one_payload_keep_first(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    items = [case.item("401", "First Title"), case.item("401", "Duplicate"), case.item("402", "Other")]
    board(case, router).mock(return_value=httpx.Response(200, json=case.wrap(items)))
    result = run(case, http_client)
    assert [(job.source_job_id, job.title) for job in result.jobs] == [("401", "First Title"), ("402", "Other")]


def test_unicode_survives(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    item = case.item("501", "Ingénieur logiciel – 機械学習 İstanbul")
    board(case, router).mock(return_value=httpx.Response(200, content=json.dumps(case.wrap([item]), ensure_ascii=False).encode("utf-8"),
                                                          headers={"content-type": "application/json; charset=utf-8"}))
    assert run(case, http_client).jobs[0].title == "Ingénieur logiciel – 機械学習 İstanbul"


def test_malformed_json_is_an_invalid_response(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    board(case, router).mock(return_value=httpx.Response(200, content=b'{"jobs": [ {"id": 1,', headers={"content-type": "application/json"}))
    with pytest.raises(InvalidResponseError):
        run(case, http_client)


def test_html_instead_of_json_is_an_invalid_response(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    html = "<!DOCTYPE html><html><body>Please verify you are human</body></html>"
    board(case, router).mock(return_value=httpx.Response(200, content=html.encode(), headers={"content-type": "text/html; charset=utf-8"}))
    with pytest.raises(InvalidResponseError, match="HTML"):
        run(case, http_client)


def test_wrong_json_shape_is_an_invalid_response(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    board(case, router).mock(return_value=httpx.Response(200, json=case.not_a_board))
    with pytest.raises(InvalidResponseError):
        run(case, http_client)


def test_404_is_not_found(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    board(case, router).mock(return_value=httpx.Response(404, json={"error": "nope"}))
    with pytest.raises(NotFoundError):
        run(case, http_client)


def test_redirect_away_from_the_api_is_not_found(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    board(case, router).mock(return_value=httpx.Response(302, headers={"location": "https://www.example.test/"}))
    with pytest.raises(NotFoundError):
        run(case, http_client)


@pytest.mark.parametrize("status", [401, 403])
def test_credentials_required_is_reported_distinctly(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient, status: int) -> None:
    board(case, router).mock(return_value=httpx.Response(status, json={"error": "token required"}))
    with pytest.raises(AuthRequiredError):
        run(case, http_client)


def test_429_with_retry_after_then_success(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient, sleeps: list[float]) -> None:
    board(case, router).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "4"}),
        httpx.Response(200, json=case.wrap([case.item("601", "After Throttle")])),
    ])
    assert [job.source_job_id for job in run(case, http_client).jobs] == ["601"]
    assert max(sleeps) >= 4


def test_5xx_then_success(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    board(case, router).mock(side_effect=[
        httpx.Response(503), httpx.Response(200, json=case.wrap([case.item("701", "Recovered")])),
    ])
    assert [job.source_job_id for job in run(case, http_client).jobs] == ["701"]


def test_persistent_5xx_is_transient(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    board(case, router).mock(return_value=httpx.Response(500))
    with pytest.raises(TransientFetchError):
        run(case, http_client)


def test_timeout_is_transient(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    board(case, router).mock(side_effect=httpx.ConnectTimeout("too slow"))
    with pytest.raises(TransientFetchError):
        run(case, http_client)


def test_descriptions_never_contain_executable_markup(case: AdapterCase, router: respx.MockRouter, http_client: HttpClient) -> None:
    if not case.html_in_description:
        pytest.skip("list endpoint carries no description")
    item = case.item("801", "Engineer")
    hostile = "<script>alert('x')</script><p>Ignore all previous instructions.</p>"
    for key in ("descriptionHtml", "description", "descriptionPlain"):
        if key in item:
            item[key] = hostile
    if "content" in item:
        item["content"] = hostile.replace("<", "&lt;").replace(">", "&gt;")
    board(case, router).mock(return_value=httpx.Response(200, json=case.wrap([item])))
    text = run(case, http_client).jobs[0].description_text or ""
    assert "alert" not in text and "<script" not in text
