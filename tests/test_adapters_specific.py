"""Adapter-specific behavior, mostly driven by trimmed real payloads in tests/fixtures/."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from jobhunt.errors import AuthRequiredError, InvalidResponseError, NotFoundError
from jobhunt.http import HttpClient
from jobhunt.models import CompanyRef, EmploymentType, ExperienceLevel
from jobhunt.sources.ashby import AshbySource
from jobhunt.sources.base import FetchContext
from jobhunt.sources.greenhouse import GreenhouseSource
from jobhunt.sources.lever import LeverSource
from jobhunt.sources.recruitee import RecruiteeSource
from jobhunt.sources.smartrecruiters import SmartRecruitersSource
from jobhunt.sources.workable import WorkableSource

Fixture = Callable[[str], Any]


def company(ats: str, slug: str = "acme", region: str | None = None) -> CompanyRef:
    return CompanyRef(id=1, name="Acme", ats_type=ats, slug=slug, region=region, status="active")


NO_DETAIL = FetchContext(wants_detail=lambda job: False)


class TestGreenhouse:
    def test_real_sample(self, router: respx.MockRouter, http_client: HttpClient, load_fixture: Fixture) -> None:
        route = router.get(url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/stripe/jobs.*").mock(
            return_value=httpx.Response(200, json=load_fixture("greenhouse/real_sample.json"))
        )
        result = GreenhouseSource().fetch_company(company("greenhouse", "stripe"), http_client, NO_DETAIL)
        assert route.calls.last.request.url.params["content"] == "true"
        assert len(result.jobs) == 3 and result.company_name == "Stripe" and result.total_reported == 3
        first = result.jobs[0]
        assert first.title == "Abuse Investigator"
        assert first.location_raw == "Seattle, San Francisco, New York City"
        assert first.posted_at == "2026-09-09T14:50:29Z"             # -04:00 offset converted to UTC
        assert first.apply_url == "https://stripe.com/jobs/search?gh_jid=8172510"
        assert first.description_text and "<" not in first.description_text and "&lt;" not in first.description_text
        assert first.raw is not None and "content" not in first.raw and first.raw["id"] == 8172510

    def test_missing_absolute_url_falls_back_to_board_url(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        router.get(url__regex=r".*greenhouse.*").mock(
            return_value=httpx.Response(200, json={"jobs": [{"id": 9, "title": "Engineer", "location": None}]})
        )
        job = GreenhouseSource().fetch_company(company("greenhouse"), http_client, NO_DETAIL).jobs[0]
        assert job.apply_url == "https://boards.greenhouse.io/acme/jobs/9" and job.location_raw is None


class TestLever:
    URL_US = r"https://api\.lever\.co/v0/postings/acme.*"
    URL_EU = r"https://api\.eu\.lever\.co/v0/postings/acme.*"

    def test_real_sample(self, router: respx.MockRouter, http_client: HttpClient, load_fixture: Fixture) -> None:
        router.get(url__regex=r"https://api\.lever\.co/v0/postings/leverdemo.*").mock(
            return_value=httpx.Response(200, json=load_fixture("lever/real_sample.json"))
        )
        result = LeverSource().fetch_company(company("lever", "leverdemo"), http_client, NO_DETAIL)
        assert len(result.jobs) == 3 and result.region == "us"
        first, second, third = result.jobs
        assert first.title == "Customer Success Manager"
        assert first.location_raw == "Atlanta, Georgia"
        assert first.country == "US"
        assert first.is_remote is False                          # workplaceType hybrid
        assert first.employment_type is EmploymentType.FULL_TIME
        assert (first.salary_min, first.salary_max, first.salary_currency) == (85000, 175000, "USD")
        assert first.posted_at == "2026-08-31T18:12:19Z"
        assert first.apply_url == "https://jobs.lever.co/leverdemo/ad208490-4052-4f91-a57e-433f2d1e484b"
        assert second.is_remote is False                          # workplaceType onsite
        assert third.salary_min is None and third.salary_currency is None   # no salaryRange at all

    def test_unknown_region_falls_back_to_eu_on_404(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        us = router.get(url__regex=self.URL_US).mock(return_value=httpx.Response(404, json={"ok": False}))
        eu = router.get(url__regex=self.URL_EU).mock(
            return_value=httpx.Response(200, json=[{"id": "e1", "text": "Engineer", "categories": {}}])
        )
        result = LeverSource().fetch_company(company("lever", region=None), http_client, NO_DETAIL)
        assert us.call_count == 1 and eu.call_count == 1
        assert result.region == "eu" and result.jobs[0].source_job_id == "e1"

    def test_known_eu_region_skips_the_us_host(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        us = router.get(url__regex=self.URL_US).mock(return_value=httpx.Response(200, json=[]))
        router.get(url__regex=self.URL_EU).mock(return_value=httpx.Response(200, json=[]))
        result = LeverSource().fetch_company(company("lever", region="eu"), http_client, NO_DETAIL)
        assert us.call_count == 0 and result.region == "eu"

    def test_404_on_both_hosts_is_not_found(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        router.get(url__regex=r"https://api\.(eu\.)?lever\.co/.*").mock(return_value=httpx.Response(404, json={"ok": False}))
        with pytest.raises(NotFoundError):
            LeverSource().fetch_company(company("lever"), http_client, NO_DETAIL)

    def test_html_once_is_retried_then_succeeds(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        route = router.get(url__regex=self.URL_US).mock(side_effect=[
            httpx.Response(200, content=b"<html><body>Just a moment...</body></html>", headers={"content-type": "text/html"}),
            httpx.Response(200, json=[{"id": "ok1", "text": "Engineer", "categories": {}}]),
        ])
        result = LeverSource().fetch_company(company("lever", region="us"), http_client, NO_DETAIL)
        assert route.call_count == 2 and [job.source_job_id for job in result.jobs] == ["ok1"]

    def test_html_twice_is_a_failure_not_an_empty_board(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        route = router.get(url__regex=self.URL_US).mock(
            return_value=httpx.Response(200, content=b"<!doctype html><html></html>", headers={"content-type": "text/html"})
        )
        with pytest.raises(InvalidResponseError):
            LeverSource().fetch_company(company("lever", region="us"), http_client, NO_DETAIL)
        assert route.call_count == 2

    def test_pagination_collects_every_page(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        first_page = [{"id": f"p1-{n}", "text": f"Engineer {n}", "categories": {}} for n in range(100)]
        second_page = [{"id": f"p2-{n}", "text": f"Engineer {n}", "categories": {}} for n in range(3)]
        route = router.get(url__regex=self.URL_US).mock(
            side_effect=[httpx.Response(200, json=first_page), httpx.Response(200, json=second_page)]
        )
        result = LeverSource().fetch_company(company("lever", region="us"), http_client, NO_DETAIL)
        assert len(result.jobs) == 103
        assert route.calls[1].request.url.params["skip"] == "100"

    def test_server_ignoring_skip_does_not_loop_forever(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        same_page = [{"id": f"x{n}", "text": "Engineer", "categories": {}} for n in range(100)]
        route = router.get(url__regex=self.URL_US).mock(return_value=httpx.Response(200, json=same_page))
        result = LeverSource().fetch_company(company("lever", region="us"), http_client, NO_DETAIL)
        assert len(result.jobs) == 100 and route.call_count == 2

    def test_description_combines_plain_text_lists_and_closing(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        item = {
            "id": "d1", "text": "Engineer", "categories": {},
            "descriptionPlain": "Intro paragraph.",
            "lists": [{"text": "Requirements", "content": "<li>Python</li><li>SQL</li>"}],
            "additionalPlain": "We are an equal opportunity employer.",
        }
        router.get(url__regex=self.URL_US).mock(return_value=httpx.Response(200, json=[item]))
        text = LeverSource().fetch_company(company("lever", region="us"), http_client, NO_DETAIL).jobs[0].description_text
        assert text == "Intro paragraph.\n\nRequirements\n- Python\n- SQL\n\nWe are an equal opportunity employer."


class TestAshby:
    URL = r"https://api\.ashbyhq\.com/posting-api/job-board/ashby.*"

    def test_real_sample(self, router: respx.MockRouter, http_client: HttpClient, load_fixture: Fixture) -> None:
        route = router.get(url__regex=self.URL).mock(return_value=httpx.Response(200, json=load_fixture("ashby/real_sample.json")))
        result = AshbySource().fetch_company(company("ashby", "ashby"), http_client, NO_DETAIL)
        assert route.calls.last.request.url.params["includeCompensation"] == "true"
        assert len(result.jobs) == 3
        first = result.jobs[0]
        assert first.source_job_id == "7458d4e9-da2e-47bd-98cb-adfda43d42b2"
        assert first.is_remote is True and first.employment_type is EmploymentType.FULL_TIME
        assert first.apply_url == "https://jobs.ashbyhq.com/ashby/7458d4e9-da2e-47bd-98cb-adfda43d42b2"
        assert first.posted_at == "2024-03-04T14:29:08Z"
        assert first.description_text and "<" not in first.description_text

    def test_unlisted_jobs_are_skipped(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        jobs = [
            {"id": "a", "title": "Public", "isListed": True},
            {"id": "b", "title": "Hidden", "isListed": False},
        ]
        router.get(url__regex=self.URL).mock(return_value=httpx.Response(200, json={"jobs": jobs}))
        result = AshbySource().fetch_company(company("ashby", "ashby"), http_client, NO_DETAIL)
        assert [job.source_job_id for job in result.jobs] == ["a"]

    def test_salary_component_is_annualized(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        compensation = {
            "compensationTiers": [{"components": [
                {"compensationType": "EquityPercentage", "interval": "NONE", "minValue": 0.1, "maxValue": 0.2},
                {"compensationType": "Salary", "interval": "1 HOUR", "currencyCode": "USD", "minValue": 50, "maxValue": 60},
            ]}],
        }
        router.get(url__regex=self.URL).mock(
            return_value=httpx.Response(200, json={"jobs": [{"id": "s", "title": "Engineer", "compensation": compensation}]})
        )
        job = AshbySource().fetch_company(company("ashby", "ashby"), http_client, NO_DETAIL).jobs[0]
        assert (job.salary_min, job.salary_max, job.salary_currency) == (104000.0, 124800.0, "USD")

    def test_salary_falls_back_to_summary_text(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        compensation = {"scrapeableCompensationSalarySummary": "$160K - $225K", "compensationTiers": []}
        router.get(url__regex=self.URL).mock(
            return_value=httpx.Response(200, json={"jobs": [{"id": "s", "title": "Engineer", "compensation": compensation}]})
        )
        job = AshbySource().fetch_company(company("ashby", "ashby"), http_client, NO_DETAIL).jobs[0]
        assert (job.salary_min, job.salary_max, job.salary_currency) == (160000, 225000, "USD")

    def test_secondary_locations_are_kept_and_id_falls_back_to_url(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        job_item = {
            "title": "Engineer", "location": "Berlin", "jobUrl": "https://jobs.ashbyhq.com/ashby/11111111-2222-3333-4444-555555555555",
            "secondaryLocations": [{"location": "London"}, {"location": "berlin"}, {"location": "Remote - US"}],
        }
        router.get(url__regex=self.URL).mock(return_value=httpx.Response(200, json={"jobs": [job_item]}))
        job = AshbySource().fetch_company(company("ashby", "ashby"), http_client, NO_DETAIL).jobs[0]
        assert job.source_job_id == "11111111-2222-3333-4444-555555555555"
        assert job.location_raw == "Berlin; London; Remote - US"


class TestWorkable:
    def test_real_sample(self, router: respx.MockRouter, http_client: HttpClient, load_fixture: Fixture) -> None:
        route = router.get(url__regex=r"https://apply\.workable\.com/api/v1/widget/accounts/huggingface.*").mock(
            return_value=httpx.Response(200, json=load_fixture("workable/real_sample.json"))
        )
        result = WorkableSource().fetch_company(company("workable", "huggingface"), http_client, NO_DETAIL)
        assert route.calls.last.request.url.params["details"] == "true"
        assert result.company_name == "Hugging Face" and len(result.jobs) == 3
        first = result.jobs[0]
        assert first.source_job_id == "81B46579FE"
        assert first.is_remote is True and first.country == "FR" and first.city == "Paris"
        assert first.employment_type is EmploymentType.FULL_TIME
        assert first.posted_at == "2026-05-29T00:00:00Z"
        assert first.apply_url == "https://apply.workable.com/j/81B46579FE"

    def test_unknown_account_302_is_not_found(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        router.get(url__regex=r"https://apply\.workable\.com/.*").mock(
            return_value=httpx.Response(302, headers={"location": "https://www.workable.com/"})
        )
        with pytest.raises(NotFoundError):
            WorkableSource().fetch_company(company("workable", "nope"), http_client, NO_DETAIL)

    def test_hidden_locations_are_not_exposed(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        item = {"shortcode": "H1", "title": "Engineer", "locations": [
            {"city": "Secret", "country": "Narnia", "countryCode": "NA", "hidden": True},
            {"city": "Pune", "country": "India", "countryCode": "IN", "hidden": False},
        ]}
        router.get(url__regex=r"https://apply\.workable\.com/.*").mock(return_value=httpx.Response(200, json={"name": "A", "jobs": [item]}))
        job = WorkableSource().fetch_company(company("workable"), http_client, NO_DETAIL).jobs[0]
        assert job.location_raw == "Pune, India" and job.country == "IN"


class TestSmartRecruiters:
    LIST_URL = r"https://api\.smartrecruiters\.com/v1/companies/BoschGroup/postings\?.*"
    DETAIL_URL = r"https://api\.smartrecruiters\.com/v1/companies/BoschGroup/postings/\d+$"

    def test_real_list_sample_maps_levels_and_locations(self, router: respx.MockRouter, http_client: HttpClient, load_fixture: Fixture) -> None:
        route = router.get(url__regex=self.LIST_URL).mock(return_value=httpx.Response(200, json=load_fixture("smartrecruiters/real_list_sample.json")))
        result = SmartRecruitersSource().fetch_company(company("smartrecruiters", "BoschGroup"), http_client, NO_DETAIL)
        assert route.calls.last.request.url.params["limit"] == "100"
        assert result.total_reported == 4939 and len(result.jobs) == 3 and result.company_name == "Bosch Group"
        istanbul, german_intern, uk_intern = result.jobs
        assert istanbul.location_raw == "İstanbul, Turkey" and istanbul.country == "TR" and istanbul.is_remote is False
        assert istanbul.apply_url == "https://jobs.smartrecruiters.com/BoschGroup/744000155007949"
        assert german_intern.experience_level is ExperienceLevel.INTERN
        assert uk_intern.country == "GB" and uk_intern.posted_at and uk_intern.posted_at.endswith("Z")

    def test_pagination_walks_offsets(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        def page(start: int, count: int) -> dict[str, Any]:
            items = [{"id": str(start + n), "name": f"Engineer {start + n}", "company": {"identifier": "BoschGroup"}} for n in range(count)]
            return {"offset": start, "limit": 100, "totalFound": 120, "content": items}

        route = router.get(url__regex=self.LIST_URL).mock(
            side_effect=[httpx.Response(200, json=page(1000, 100)), httpx.Response(200, json=page(1100, 20))]
        )
        result = SmartRecruitersSource().fetch_company(company("smartrecruiters", "BoschGroup"), http_client, NO_DETAIL)
        assert len(result.jobs) == 120
        assert [call.request.url.params["offset"] for call in route.calls] == ["0", "100"]

    def test_details_fill_descriptions_only_for_wanted_jobs(self, router: respx.MockRouter, http_client: HttpClient, load_fixture: Fixture) -> None:
        router.get(url__regex=self.LIST_URL).mock(return_value=httpx.Response(200, json=load_fixture("smartrecruiters/real_list_sample.json")))
        detail = router.get(url__regex=self.DETAIL_URL).mock(return_value=httpx.Response(200, json=load_fixture("smartrecruiters/real_detail_sample.json")))
        context = FetchContext(wants_detail=lambda job: job.source_job_id == "744000155007949", detail_cap=10)
        result = SmartRecruitersSource().fetch_company(company("smartrecruiters", "BoschGroup"), http_client, context)
        assert detail.call_count == 1
        hydrated, other, _ = result.jobs
        assert hydrated.needs_description is False and hydrated.description_text
        assert "Job Description" in hydrated.description_text and "<" not in hydrated.description_text
        assert hydrated.apply_url == "https://jobs.smartrecruiters.com/BoschGroup/744000155007949-brand-manager"
        assert other.needs_description is True and other.description_text is None

    def test_detail_failure_keeps_the_job_and_warns(self, router: respx.MockRouter, http_client: HttpClient, load_fixture: Fixture) -> None:
        router.get(url__regex=self.LIST_URL).mock(return_value=httpx.Response(200, json=load_fixture("smartrecruiters/real_list_sample.json")))
        router.get(url__regex=self.DETAIL_URL).mock(return_value=httpx.Response(404, json={"message": "gone"}))
        context = FetchContext(wants_detail=lambda job: True, detail_cap=10)
        result = SmartRecruitersSource().fetch_company(company("smartrecruiters", "BoschGroup"), http_client, context)
        assert len(result.jobs) == 3 and all(job.needs_description for job in result.jobs)
        assert len(result.warnings) == 3

    def test_detail_cap_limits_requests(self, router: respx.MockRouter, http_client: HttpClient, load_fixture: Fixture) -> None:
        router.get(url__regex=self.LIST_URL).mock(return_value=httpx.Response(200, json=load_fixture("smartrecruiters/real_list_sample.json")))
        detail = router.get(url__regex=self.DETAIL_URL).mock(return_value=httpx.Response(200, json=load_fixture("smartrecruiters/real_detail_sample.json")))
        context = FetchContext(wants_detail=lambda job: True, detail_cap=2)
        result = SmartRecruitersSource().fetch_company(company("smartrecruiters", "BoschGroup"), http_client, context)
        assert detail.call_count == 2 and any("capped" in warning for warning in result.warnings)

    def test_empty_200_does_not_prove_the_board_exists(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        router.get(url__regex=r"https://api\.smartrecruiters\.com/.*").mock(
            return_value=httpx.Response(200, json={"offset": 0, "limit": 100, "totalFound": 0, "content": []})
        )
        source = SmartRecruitersSource()
        result = source.fetch_company(company("smartrecruiters", "ghost"), http_client, NO_DETAIL)
        assert result.jobs == [] and source.proves_board_exists(result) is False


class TestRecruitee:
    URL = r"https://bunq\.recruitee\.com/api/offers/.*"

    def test_real_sample(self, router: respx.MockRouter, http_client: HttpClient, load_fixture: Fixture) -> None:
        router.get(url__regex=self.URL).mock(return_value=httpx.Response(200, json=load_fixture("recruitee/real_sample.json")))
        result = RecruiteeSource().fetch_company(company("recruitee", "bunq"), http_client, NO_DETAIL)
        assert len(result.jobs) == 3 and result.company_name == "bunq"
        first = result.jobs[0]
        assert first.source_job_id == "2771751" and first.title == "Deputy AML Manager - Belgium"
        assert first.country == "BE" and first.city == "Brussels" and first.is_remote is False
        assert first.employment_type is EmploymentType.FULL_TIME
        assert first.posted_at == "2026-10-05T16:05:18Z"
        assert first.apply_url == "https://careers.bunq.com/o/deputy-aml-manager-belgium"
        assert first.salary_min is None and first.salary_currency is None
        assert first.description_text and "<" not in first.description_text

    def test_future_token_requirement_is_reported_as_blocked_source(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        router.get(url__regex=self.URL).mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))
        with pytest.raises(AuthRequiredError):
            RecruiteeSource().fetch_company(company("recruitee", "bunq"), http_client, NO_DETAIL)

    @pytest.mark.parametrize("slug", ["evil.example.com/x", "a b", "-bad", "ok_underscore", ""])
    def test_slug_is_validated_before_becoming_a_hostname(self, http_client: HttpClient, slug: str) -> None:
        with pytest.raises(NotFoundError):
            RecruiteeSource().fetch_company(company("recruitee", slug), http_client, NO_DETAIL)

    def test_unpublished_offers_are_skipped_and_salary_parsed(self, router: respx.MockRouter, http_client: HttpClient) -> None:
        offers = [
            {"id": 1, "title": "Draft", "status": "draft"},
            {"id": 2, "title": "Live", "status": "published", "salary": {"min": 5000, "max": 6000, "period": "month", "currency": "EUR"}},
        ]
        router.get(url__regex=self.URL).mock(return_value=httpx.Response(200, json={"offers": offers}))
        result = RecruiteeSource().fetch_company(company("recruitee", "bunq"), http_client, NO_DETAIL)
        assert [job.source_job_id for job in result.jobs] == ["2"]
        assert (result.jobs[0].salary_min, result.jobs[0].salary_max, result.jobs[0].salary_currency) == (60000.0, 72000.0, "EUR")
