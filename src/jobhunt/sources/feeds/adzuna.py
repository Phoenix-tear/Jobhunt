"""Adzuna job search API (free key from developer.adzuna.com).

GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?app_id=..&app_key=..&what=..

Official default limits (developer.adzuna.com/docs/terms_of_service, checked 2026-10-12):
25 hits/minute, 250/day, 1,000/week, 2,500/month. Allowed uses include personal research, with
attribution to "The Adzuna API"; listings shown to people must carry the word "Adzuna" linking to
adzuna.co.uk / the local domain. We therefore:

* budget calls per run, per day, per ISO week and per month (state kept in source_state),
  rotating through country x query combinations so every combination gets served over time;
* space requests (config: host_min_interval_seconds for api.adzuna.com);
* store "Jobs by Adzuna" attribution on every sighting and keep Adzuna's redirect URL;
* mark every job `needs_description` (the API returns a truncated snippet).

The search response shape (results[].id/title/description/created/redirect_url/company.display_name/
location.display_name/salary_min/salary_max/salary_is_predicted/contract_type/contract_time) follows
Adzuna's public API documentation; it is exercised by mocked tests and confirmed on the first run
with a real key (no key = the source is skipped with a clear message, not an error).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from jobhunt.errors import InvalidResponseError
from jobhunt.http import HttpClient
from jobhunt.models import EmploymentType, FetchResult, NormalizedJob
from jobhunt.normalize import clean_text, html_to_text, to_number
from jobhunt.sources.base import FeedContext, FeedSource, as_dict, as_list, collect_jobs, load_json
from jobhunt.timeutil import to_utc_iso

logger = logging.getLogger(__name__)

DEFAULT_COUNTRIES = ["in", "us", "gb", "ca", "de", "sg", "au"]
DEFAULT_QUERIES = ["software engineer", "graduate software developer", "data scientist", "machine learning engineer", "software intern"]


class AdzunaSource(FeedSource):
    name = "adzuna"
    daily_fetch_cap = 250        # provider ceiling; `daily_call_budget` keeps us well below it
    min_interval_hours = 20.0
    stale_after_days = 30
    required_secrets = ("ADZUNA_APP_ID", "ADZUNA_APP_KEY")
    attribution = "Jobs by Adzuna (https://www.adzuna.com); data via The Adzuna API"
    base_url = "https://api.adzuna.com/v1/api/jobs"

    @property
    def fetch_cap_per_day(self) -> int | None:
        return min(int(self.option("daily_call_budget", 100)), self.daily_fetch_cap or 250)

    def fetch(self, http: HttpClient, context: FeedContext) -> FetchResult:
        countries = [str(code).lower() for code in self.option("countries", DEFAULT_COUNTRIES)]
        queries = [str(query) for query in self.option("queries", DEFAULT_QUERIES)]
        combinations = [(country, query) for query in queries for country in countries]
        if not combinations:
            return FetchResult(jobs=[], warnings=["adzuna: no countries/queries configured"])

        state = self._load_state(context)
        week_key = "{}-W{:02d}".format(*context.now.isocalendar()[:2])
        month_key = context.now.strftime("%Y-%m")
        week_calls = state["week_calls"] if state.get("week") == week_key else 0
        month_calls = state["month_calls"] if state.get("month") == month_key else 0

        allowance = min(
            int(self.option("calls_per_run", 40)),
            int(self.option("weekly_call_budget", 900)) - week_calls,
            int(self.option("monthly_call_budget", 2200)) - month_calls,
        )
        pages = max(1, int(self.option("pages_per_query", 1)))
        per_page = min(int(self.option("results_per_page", 50)), 50)
        max_days_old = int(self.option("max_days_old", 14))

        jobs: list[NormalizedJob] = []
        warnings: list[str] = []
        malformed = 0
        calls = 0
        index = int(state.get("next_index", 0)) % len(combinations)
        served = 0

        while served < len(combinations) and calls < allowance:
            country, query = combinations[index]
            for page in range(1, pages + 1):
                if calls >= allowance or not context.budget.take():
                    break
                calls += 1
                results = self._search(http, country, query, page, per_page, max_days_old)
                converted, bad = collect_jobs(results, lambda item, c=country: self._to_job(item, c))
                jobs.extend(converted)
                malformed += bad
                if len(results) < per_page:
                    break
            served += 1
            index = (index + 1) % len(combinations)

        if served < len(combinations):
            warnings.append(f"adzuna: budget served {served}/{len(combinations)} country x query combinations; "
                            f"the rest rotate in on later runs")
        logger.info("adzuna: %d calls this run (week %d, month %d before)", calls, week_calls, month_calls)

        new_state = {
            "next_index": index, "week": week_key, "week_calls": week_calls + calls,
            "month": month_key, "month_calls": month_calls + calls,
        }
        unique = {job.source_job_id: job for job in jobs}
        return FetchResult(
            jobs=list(unique.values()), malformed_items=malformed, warnings=warnings,
            cursor=json.dumps(new_state, separators=(",", ":")),
        )

    # -- internals -------------------------------------------------------------------------

    @staticmethod
    def _load_state(context: FeedContext) -> dict[str, Any]:
        try:
            value = json.loads(context.cursor) if context.cursor else {}
        except ValueError:
            value = {}
        return value if isinstance(value, dict) else {}

    def _search(self, http: HttpClient, country: str, query: str, page: int, per_page: int, max_days_old: int) -> list[Any]:
        params = {
            "app_id": self.secret("ADZUNA_APP_ID"),
            "app_key": self.secret("ADZUNA_APP_KEY"),
            "results_per_page": per_page,
            "what": query,
            "max_days_old": max_days_old,
            "sort_by": "date",
            "content-type": "application/json",
        }
        url = f"{self.base_url}/{country}/search/{page}"
        payload = load_json(http.get(url, params=params, headers={"Accept": "application/json"}), what=f"adzuna/{country}")
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise InvalidResponseError(f"adzuna/{country}: missing 'results' list")
        return payload["results"]

    def _to_job(self, item: Any, country: str) -> NormalizedJob | None:
        if not isinstance(item, dict) or item.get("id") is None:
            return None
        title = clean_text(html_to_text(item.get("title")))
        url = clean_text(item.get("redirect_url"))
        if not title or not url:
            return None
        location = as_dict(item.get("location"))
        predicted = str(item.get("salary_is_predicted", "0")) in {"1", "true", "True"}
        salary_min = None if predicted else to_number(item.get("salary_min"))
        salary_max = None if predicted else to_number(item.get("salary_max"))
        contract_time = clean_text(item.get("contract_time")) or ""
        contract_type = clean_text(item.get("contract_type")) or ""
        employment = EmploymentType.UNKNOWN
        if "part" in contract_time:
            employment = EmploymentType.PART_TIME
        elif contract_type == "contract":
            employment = EmploymentType.CONTRACT
        elif "full" in contract_time or contract_type == "permanent":
            employment = EmploymentType.FULL_TIME
        areas = [clean_text(part) for part in as_list(location.get("area"))]
        snippet = html_to_text(item.get("description"))
        return NormalizedJob(
            source=self.name, source_job_id=str(item["id"]), title=title,
            company_name=clean_text(as_dict(item.get("company")).get("display_name")) or "",
            apply_url=url, source_url=url,
            location_raw=clean_text(location.get("display_name")) or ", ".join(part for part in areas if part) or None,
            country=country.upper() if len(country) == 2 else None,
            employment_type=employment,
            description_text=snippet or None, needs_description=True,  # truncated by Adzuna
            posted_at=to_utc_iso(item.get("created")),
            salary_min=salary_min, salary_max=salary_max,
            salary_currency=_CURRENCY_BY_COUNTRY.get(country) if (salary_min or salary_max) else None,
            attribution_text=self.attribution,
            raw={key: value for key, value in item.items() if key != "description"},
        )


# Adzuna reports salaries in each country's local currency.
_CURRENCY_BY_COUNTRY = {
    "in": "INR", "us": "USD", "gb": "GBP", "ca": "CAD", "de": "EUR", "sg": "SGD", "au": "AUD",
    "fr": "EUR", "nl": "EUR", "at": "EUR", "be": "EUR", "it": "EUR", "es": "EUR", "pl": "PLN",
    "br": "BRL", "mx": "MXN", "nz": "NZD", "za": "ZAR", "ch": "CHF",
}
