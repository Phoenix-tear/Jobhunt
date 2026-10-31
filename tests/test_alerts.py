"""Phase 4: alert parsing per platform, Gmail client safety, the ingest-email pipeline."""

from __future__ import annotations

import base64
import inspect
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.alerts import gmail
from jobhunt.alerts.gmail import (
    API_BASE, READONLY_SCOPE, GmailApiClient, MailAuthError, RawEmail, TokenStore, message_from_api,
    message_from_eml, sender_allowed,
)
from jobhunt.alerts.parsing import parse_alert_html, parse_alert_text
from jobhunt.alerts.pipeline import build_query, ingest_emails, match_platform
from jobhunt.alerts.urls import PLATFORMS, recognize
from jobhunt.cli import main
from jobhunt.config import Config
from jobhunt.http import HttpClient
from jobhunt.locations import LocationParser
from jobhunt.sources import build_ats_sources
from tests.conftest import FIXTURE_DIR, FakeClock

EMAIL_DIR = FIXTURE_DIR / "email"
RECEIVED = datetime(2026, 10, 12, 6, 30, tzinfo=timezone.utc)


def fixture_html(name: str) -> str:
    return (EMAIL_DIR / f"{name}.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def locations(config: Config) -> LocationParser:
    return LocationParser(config.classification.extra_city_countries)


# (title, company, location, canonical url) per fixture, in email order
EXPECTED: dict[str, list[tuple[str, str, str | None, str]]] = {
    "linkedin": [
        ("Software Engineer, New Grad", "Acme Technologies", "Bengaluru, Karnataka, India", "https://www.linkedin.com/jobs/view/3912345678"),
        ("Backend Developer Intern", "Globex Corporation", "Remote", "https://www.linkedin.com/jobs/view/3912399999"),
        ("Data Analyst", "Initech", "Hyderabad, Telangana, India", "https://www.linkedin.com/jobs/view/3912300001"),
    ],
    "naukri": [
        ("Software Engineer I", "Acme Technologies", "Pune, Maharashtra",
         "https://www.naukri.com/job-listings-software-engineer-i-acme-technologies-pune-0-to-2-years-120924501234"),
        ("Python Developer", "Globex Software Solutions", "Bengaluru",
         "https://www.naukri.com/job-listings-python-developer-globex-software-bengaluru-bangalore-1-to-3-years-120924509999"),
    ],
    "internshala": [
        ("Software Development", "Acme Labs", "Work From Home",
         "https://internshala.com/internship/detail/software-development-work-from-home-internship-at-acme-labs1728000000"),
        ("Machine Learning", "Globex AI", "Bangalore",
         "https://internshala.com/internship/detail/machine-learning-internship-in-bangalore-at-globex-ai1727999999"),
    ],
    "indeed": [
        ("Junior Python Developer", "Acme Software Pvt Ltd", "Hyderabad, Telangana", "https://in.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b"),
        ("Associate Software Engineer", "Globex India", "Hyderabad", "https://in.indeed.com/viewjob?jk=ffeeddccbbaa9988"),
    ],
    "wellfound": [
        ("Backend Engineer (New Grad)", "Rocket AI", "San Francisco · Remote", "https://wellfound.com/jobs/3456789-backend-engineer-new-grad"),
        ("Machine Learning Engineer", "Globex Labs", "Bengaluru · Hybrid", "https://wellfound.com/jobs/3456790-machine-learning-engineer"),
    ],
    "unstop": [
        ("Data Science Intern", "Acme Analytics", "Remote", "https://unstop.com/internships/data-science-intern-acme-analytics-1234567"),
        ("Graduate Engineer Trainee", "Globex Industries", "Chennai, Tamil Nadu", "https://unstop.com/jobs/graduate-engineer-trainee-globex-industries-7654321"),
    ],
    "foundit": [
        ("Software Developer - Java", "Acme Solutions", "Noida, Uttar Pradesh",
         "https://www.foundit.in/job/software-developer-java-acme-solutions-noida-uttar-pradesh-12345678"),
        ("QA Engineer", "Globex Systems", "Mumbai", "https://www.foundit.in/job/qa-engineer-globex-systems-mumbai-maharashtra-87654321"),
    ],
    "cutshort": [
        ("Backend Engineer", "Acme Fintech", "Pune", "https://cutshort.io/job/Backend-Engineer-Pune-Acme-Fintech-Ab3Cd9Ef"),
        ("Full Stack Developer", "Globex Apps", "Remote", "https://cutshort.io/job/Full-Stack-Developer-Remote-Globex-Apps-Zx8Yw7Vu"),
    ],
    "instahyre": [
        ("Software Engineer", "Acme Systems", "Bangalore", "https://www.instahyre.com/job-123456-software-engineer-at-acme-bangalore/"),
        ("Data Engineer", "Globex", "Gurgaon", "https://www.instahyre.com/job-654321-data-engineer-at-globex-gurgaon/"),
    ],
    "glassdoor": [
        ("Associate Software Engineer", "Acme Corp", "Gurgaon, Haryana", "https://www.glassdoor.com/job-listing/?jl=1009876543210"),
        ("Data Scientist", "Globex", "Bengaluru", "https://www.glassdoor.com/job-listing/?jl=1009111222333"),
    ],
}


class TestPlatformParsers:
    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_fixture_email_yields_the_expected_cards(self, platform: str, locations: LocationParser, router: respx.MockRouter) -> None:
        jobs = parse_alert_html(platform, fixture_html(platform), locations)
        assert [(j.title, j.company, j.location, j.url) for j in jobs] == EXPECTED[platform]
        assert all(job.recognized and job.job_id for job in jobs)
        assert router.calls.call_count == 0     # parsing never touches the network (unmocked requests would raise)

    def test_every_platform_has_a_fixture_and_an_expectation(self) -> None:
        assert set(EXPECTED) == set(PLATFORMS)

    def test_non_job_links_are_ignored(self, locations: LocationParser) -> None:
        jobs = parse_alert_html("linkedin", fixture_html("linkedin"), locations)
        assert not any("unsubscribe" in j.url or "search" in j.url or "help" in j.url for j in jobs)
        unstop = parse_alert_html("unstop", fixture_html("unstop"), locations)
        assert all("competitions" not in j.url for j in unstop)           # hackathons are not jobs
        internshala = parse_alert_html("internshala", fixture_html("internshala"), locations)
        assert all("dashboard" not in j.url for j in internshala)

    def test_ids_are_the_platform_job_ids(self, locations: LocationParser) -> None:
        linkedin = parse_alert_html("linkedin", fixture_html("linkedin"), locations)
        assert [j.job_id for j in linkedin] == ["3912345678", "3912399999", "3912300001"]
        indeed = parse_alert_html("indeed", fixture_html("indeed"), locations)
        assert [j.job_id for j in indeed] == ["0a1b2c3d4e5f6a7b", "ffeeddccbbaa9988"]

    def test_ats_links_are_recognized_with_their_slug_and_job_id(self, locations: LocationParser) -> None:
        jobs = parse_alert_html("wellfound", fixture_html("wellfound_ats_links"), locations)
        greenhouse, lever, native = jobs
        assert (greenhouse.ats.ats_type.value, greenhouse.ats.slug, greenhouse.ats.job_id) == ("greenhouse", "rocketai", "4455667")
        assert greenhouse.url == "https://boards.greenhouse.io/rocketai/jobs/4455667"     # tracking stripped
        assert (lever.ats.ats_type.value, lever.ats.slug, lever.ats.region) == ("lever", "globexlabs", "us")
        assert lever.url == "https://jobs.lever.co/globexlabs/0f0c4a52-1a2b-4c3d-8e9f-aabbccddeeff"
        assert native.ats is None

    def test_unknown_link_shape_on_a_platform_host_is_kept_verbatim(self, locations: LocationParser) -> None:
        html = ('<div><a href="https://www.naukri.com/mailers/redirect/998877665?u=abc">Senior Data Engineer II</a>'
                "<p>Initech</p><p>Pune</p></div>")
        (job,) = parse_alert_html("naukri", html, locations)
        assert job.recognized is False and job.url == "https://www.naukri.com/mailers/redirect/998877665?u=abc"
        assert (job.title, job.company, job.location) == ("Senior Data Engineer II", "Initech", "Pune")

    def test_hostile_email_content_is_inert_data(self, locations: LocationParser, router: respx.MockRouter) -> None:
        jobs = parse_alert_html("linkedin", fixture_html("linkedin_hostile"), locations)
        assert [j.url for j in jobs] == ["https://www.linkedin.com/jobs/view/3912000042"]   # phishing link ignored
        assert jobs[0].company == "Evil Corp"
        assert "attacker@evil.test" not in json.dumps([j.__dict__ for j in jobs], default=str)
        assert router.calls.call_count == 0

    def test_empty_garbage_and_non_html_inputs(self, locations: LocationParser) -> None:
        for html in ["", "   ", "plain words only", "<html></html>", "<a>no href</a>", "<<<>>>", "\x00\x01"]:
            assert parse_alert_html("linkedin", html, locations) == []

    def test_plain_text_fallback(self, locations: LocationParser) -> None:
        text = ("Software Engineer, New Grad\nAcme Technologies\nBengaluru, India\n"
                "https://www.linkedin.com/comm/jobs/view/3912345678?trackingId=abc\n\n"
                "Unsubscribe: https://www.linkedin.com/comm/psettings/email-unsubscribe\n")
        (job,) = parse_alert_text("linkedin", text, locations)
        assert (job.title, job.url, job.job_id) == ("Software Engineer, New Grad", "https://www.linkedin.com/jobs/view/3912345678", "3912345678")


class TestUrlShapes:
    @pytest.mark.parametrize(
        ("platform", "url", "canonical", "job_id"),
        [
            ("linkedin", "https://www.linkedin.com/comm/jobs/view/3912345678?trackingId=x", "https://www.linkedin.com/jobs/view/3912345678", "3912345678"),
            ("linkedin", "https://in.linkedin.com/jobs/view/senior-engineer-at-acme-3912345678?refId=1", "https://www.linkedin.com/jobs/view/3912345678", "3912345678"),
            ("linkedin", "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=3912345678", "https://www.linkedin.com/jobs/view/3912345678", "3912345678"),
            ("indeed", "https://www.indeed.com/rc/clk?jk=0a1b2c3d4e5f6a7b&fccid=1", "https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b", "0a1b2c3d4e5f6a7b"),
            ("indeed", "https://in.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b&tk=9", "https://in.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b", "0a1b2c3d4e5f6a7b"),
            ("naukri", "https://www.naukri.com/job-listings-qa-acme-pune-1-to-3-years-120924501234?src=x", "https://www.naukri.com/job-listings-qa-acme-pune-1-to-3-years-120924501234", "120924501234"),
            ("wellfound", "https://angel.co/company/acme/jobs/445566-backend-engineer", "https://wellfound.com/jobs/445566-backend-engineer", "445566"),
            ("glassdoor", "https://www.glassdoor.com/job-listing/x.htm?jl=1009876543210&src=y", "https://www.glassdoor.com/job-listing/?jl=1009876543210", "1009876543210"),
            ("instahyre", "https://www.instahyre.com/job-123456-software-engineer-at-acme/?utm=1", "https://www.instahyre.com/job-123456-software-engineer-at-acme/", "123456"),
        ],
    )
    def test_canonical_form_and_id(self, platform: str, url: str, canonical: str, job_id: str) -> None:
        link = recognize(platform, url)
        assert link is not None and (link.url, link.job_id) == (canonical, job_id)

    @pytest.mark.parametrize(
        ("platform", "url"),
        [
            ("linkedin", "https://www.linkedin.com/jobs/search/?keywords=python"),
            ("linkedin", "https://evil.example/jobs/view/3912345678"),
            ("linkedin", "https://www.linkedin.com.evil.example/jobs/view/3912345678"),
            ("linkedin", "ftp://www.linkedin.com/jobs/view/3912345678"),
            ("indeed", "https://www.indeed.com/viewjob?jk=short"),
            ("naukri", "https://www.naukri.com/mnjuser/profile"),
            ("glassdoor", "https://www.glassdoor.com/Overview/Working-at-Acme.htm"),
            ("linkedin", "not a url"),
        ],
    )
    def test_non_job_or_foreign_urls_are_rejected(self, platform: str, url: str) -> None:
        assert recognize(platform, url) is None


class TestSenderAllowlist:
    @pytest.mark.parametrize(
        ("sender", "allowed", "expected"),
        [
            ("jobalerts-noreply@linkedin.com", ("@linkedin.com",), True),
            ("x@mail.linkedin.com", ("@linkedin.com",), True),
            ("x@linkedin.com.evil.example", ("@linkedin.com",), False),
            ("x@notlinkedin.com", ("@linkedin.com",), False),
            ("alerts@indeed.com", ("alerts@indeed.com",), True),
            ("other@indeed.com", ("alerts@indeed.com",), False),
            ("", ("@linkedin.com",), False),
        ],
    )
    def test_matching(self, sender: str, allowed: tuple[str, ...], expected: bool) -> None:
        assert sender_allowed(sender, allowed) is expected

    def test_platform_resolution_and_query(self, config: Config) -> None:
        assert match_platform(config, "jobalerts-noreply@linkedin.com") == "linkedin"
        assert match_platform(config, "no-reply@internshala.com") == "internshala"
        assert match_platform(config, "friend@gmail.com") is None
        query = build_query(config, datetime(2026, 10, 1, tzinfo=timezone.utc))
        assert query.startswith("from:(linkedin.com OR naukri.com") and query.endswith(f"after:{int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())}")


# --- Gmail client -------------------------------------------------------------------------------

def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def api_message(message_id: str, sender: str, html: str, *, date: str = "Mon, 12 Oct 2026 06:30:00 +0000", text: str | None = None) -> dict:
    parts = [{"mimeType": "text/html", "body": {"data": b64(html)}, "headers": [{"name": "Content-Type", "value": "text/html; charset=UTF-8"}]}]
    if text is not None:
        parts.insert(0, {"mimeType": "text/plain", "body": {"data": b64(text)}})
    return {
        "id": message_id, "internalDate": str(int(RECEIVED.timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": f"Job Alerts <{sender}>"}, {"name": "Date", "value": date},
                        {"name": "Subject", "value": "your alert"}],
            "parts": parts,
        },
    }


class TestGmailClient:
    @pytest.fixture()
    def client(self, http_client: HttpClient) -> GmailApiClient:
        return GmailApiClient(http_client, lambda: "tok-123")

    def test_message_decoding(self) -> None:
        email = message_from_api(api_message("m1", "JobAlerts-NoReply@LinkedIn.com", "<p>Zürich – İstanbul</p>", text="plain"))
        assert email.sender == "jobalerts-noreply@linkedin.com"
        assert email.html == "<p>Zürich – İstanbul</p>" and email.text == "plain"
        assert email.received_at == RECEIVED

    def test_date_falls_back_to_internal_date(self) -> None:
        email = message_from_api(api_message("m1", "a@b.c", "<p>x</p>", date="garbage"))
        assert email.received_at == RECEIVED

    def test_list_paginates_and_sends_bearer_and_query(self, router: respx.MockRouter, client: GmailApiClient) -> None:
        route = router.get(f"{API_BASE}/messages").mock(side_effect=[
            httpx.Response(200, json={"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"}),
            httpx.Response(200, json={"messages": [{"id": "c"}]}),
        ])
        assert client.list_message_ids("from:(x.com) after:1", limit=10) == ["a", "b", "c"]
        first, second = route.calls[0].request, route.calls[1].request
        assert first.headers["authorization"] == "Bearer tok-123" and first.url.params["q"] == "from:(x.com) after:1"
        assert second.url.params["pageToken"] == "p2"

    def test_list_respects_the_limit_and_handles_empty_mailboxes(self, router: respx.MockRouter, client: GmailApiClient) -> None:
        router.get(f"{API_BASE}/messages").mock(return_value=httpx.Response(200, json={"resultSizeEstimate": 0}))
        assert client.list_message_ids("q", 5) == []
        router.get(f"{API_BASE}/messages").mock(return_value=httpx.Response(
            200, json={"messages": [{"id": str(n)} for n in range(10)], "nextPageToken": "more"}))
        assert len(client.list_message_ids("q", 3)) == 3

    def test_get_message_requests_full_format(self, router: respx.MockRouter, client: GmailApiClient) -> None:
        route = router.get(f"{API_BASE}/messages/abc").mock(return_value=httpx.Response(200, json=api_message("abc", "a@linkedin.com", "<p>x</p>")))
        assert client.get_message("abc").message_id == "abc"
        assert route.calls.last.request.url.params["format"] == "full"

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_failures_ask_for_reauthorization(self, router: respx.MockRouter, client: GmailApiClient, status: int) -> None:
        router.get(f"{API_BASE}/profile").mock(return_value=httpx.Response(status, json={"error": {"message": "invalid_grant"}}))
        with pytest.raises(MailAuthError, match="gmail-auth"):
            client.profile_email()

    def test_profile(self, router: respx.MockRouter, client: GmailApiClient) -> None:
        router.get(f"{API_BASE}/profile").mock(return_value=httpx.Response(200, json={"emailAddress": "Me@Gmail.com"}))
        assert client.profile_email() == "me@gmail.com"

    def test_client_is_structurally_read_only(self) -> None:
        public = {name for name, member in inspect.getmembers(GmailApiClient, inspect.isfunction) if not name.startswith("_")}
        assert public == {"profile_email", "list_message_ids", "get_message"}
        source = inspect.getsource(gmail)
        for forbidden in (".post(", ".put(", ".patch(", ".delete(", "/modify", "/trash", "/send", "batchModify", "labels"):
            assert forbidden not in source, forbidden
        assert READONLY_SCOPE == "https://www.googleapis.com/auth/gmail.readonly"
        assert "gmail.modify" not in source and "mail.google.com" not in source

    def test_only_get_requests_are_ever_sent(self, router: respx.MockRouter, client: GmailApiClient) -> None:
        router.route().mock(return_value=httpx.Response(200, json={"messages": [], "emailAddress": "a@b.c"}))
        client.list_message_ids("q", 1)
        client.profile_email()
        assert {call.request.method for call in router.calls} == {"GET"}


class TestTokenStore:
    def write_token(self, path: Path, scopes: list[str], expiry: str = "2999-01-01T00:00:00Z") -> None:
        path.write_text(json.dumps({
            "token": "access-1", "refresh_token": "refresh-1", "client_id": "cid", "client_secret": "sec",
            "token_uri": "https://oauth2.googleapis.com/token", "scopes": scopes, "expiry": expiry,
        }), encoding="utf-8")

    def test_missing_token_explains_how_to_authorize(self, tmp_path: Path) -> None:
        with pytest.raises(MailAuthError, match="gmail-auth"):
            TokenStore(tmp_path / "c.json", tmp_path / "token.json").bearer_token()

    def test_valid_readonly_token_is_used(self, tmp_path: Path) -> None:
        self.write_token(tmp_path / "token.json", [READONLY_SCOPE])
        assert TokenStore(tmp_path / "c.json", tmp_path / "token.json").bearer_token() == "access-1"

    @pytest.mark.parametrize("scopes", [["https://www.googleapis.com/auth/gmail.modify"],
                                        [READONLY_SCOPE, "https://mail.google.com/"], []])
    def test_tokens_with_any_other_scope_are_refused(self, tmp_path: Path, scopes: list[str]) -> None:
        self.write_token(tmp_path / "token.json", scopes)
        with pytest.raises(MailAuthError, match="gmail.readonly"):
            TokenStore(tmp_path / "c.json", tmp_path / "token.json").bearer_token()

    def test_rejected_refresh_token_maps_to_a_reauth_message(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from google.auth.exceptions import RefreshError
        from google.oauth2.credentials import Credentials

        self.write_token(tmp_path / "token.json", [READONLY_SCOPE], expiry="2020-01-01T00:00:00Z")

        def refuse(self_, request):  # noqa: ANN001
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

        monkeypatch.setattr(Credentials, "refresh", refuse)
        with pytest.raises(MailAuthError, match="7-day|Testing"):
            TokenStore(tmp_path / "c.json", tmp_path / "token.json").bearer_token()

    def test_corrupt_token_file(self, tmp_path: Path) -> None:
        (tmp_path / "token.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(MailAuthError, match="not valid JSON"):
            TokenStore(tmp_path / "c.json", tmp_path / "token.json").bearer_token()


# --- pipeline -----------------------------------------------------------------------------------

class FakeMailbox:
    """In-memory mailbox implementing the MailSource protocol; counts calls."""

    def __init__(self, messages: list[RawEmail]) -> None:
        self.messages = {message.message_id: message for message in messages}
        self.queries: list[str] = []
        self.fetched: list[str] = []

    def list_message_ids(self, query: str, limit: int) -> list[str]:
        self.queries.append(query)
        return list(self.messages)[:limit]

    def get_message(self, message_id: str) -> RawEmail:
        self.fetched.append(message_id)
        return self.messages[message_id]


def raw(message_id: str, sender: str, fixture: str, received: datetime = RECEIVED) -> RawEmail:
    return RawEmail(message_id, sender, received, fixture_html(fixture), "")


@pytest.fixture()
def mailbox() -> FakeMailbox:
    return FakeMailbox([
        raw("m-linkedin", "jobalerts-noreply@linkedin.com", "linkedin"),
        raw("m-naukri", "jobalert@naukri.com", "naukri"),
        raw("m-intern", "alerts@internshala.com", "internshala"),
    ])


def run_ingest(connection, config, mailbox, clock, **kwargs):
    return ingest_emails(connection, config, mailbox, clock=clock, **kwargs)


class TestPipeline:
    def test_jobs_are_stored_per_platform_source_with_attribution(self, connection, config, mailbox, clock: FakeClock, router) -> None:
        summary = run_ingest(connection, config, mailbox, clock)
        assert (summary.messages_new, summary.jobs_parsed) == (3, 7)
        rows = connection.execute(
            "SELECT s.source, j.title, j.company_name, j.country, j.city, j.needs_description, j.canonical_apply_url AS apply_url, j.employment_type, j.is_remote, s.attribution_text "
            "FROM jobs j JOIN job_sightings s ON s.job_id = j.id ORDER BY s.source, j.title"
        ).fetchall()
        by_title = {row["title"]: row for row in rows}
        grad = by_title["Software Engineer, New Grad"]
        assert grad["source"] == "email_linkedin" and (grad["country"], grad["city"]) == ("IN", "Bengaluru")
        assert grad["needs_description"] == 1 and grad["apply_url"] == "https://linkedin.com/jobs/view/3912345678"   # stored canonical form drops www.
        assert "job-alert email" in grad["attribution_text"]
        assert by_title["Backend Developer Intern"]["employment_type"] == "internship" and by_title["Backend Developer Intern"]["is_remote"] == 1
        assert by_title["Software Development"]["employment_type"] == "internship"      # /internship/ in the Internshala path
        assert router.calls.call_count == 0                                              # no page was ever requested

    def test_only_parsed_fields_are_persisted(self, connection, config, mailbox, clock) -> None:
        run_ingest(connection, config, mailbox, clock)
        dump = " ".join(str(value) for table in ("jobs", "job_sightings", "processed_emails", "companies")
                        for row in connection.execute(f"SELECT * FROM {table}") for value in tuple(row))
        assert "unsubscribe" not in dump.lower() and "<html" not in dump.lower() and "trackingId" not in dump
        assert json.loads(connection.execute("SELECT raw_json FROM jobs LIMIT 1").fetchone()[0]).keys() == {"platform", "link_recognized"}

    def test_reprocessing_is_idempotent(self, connection, config, mailbox, clock: FakeClock) -> None:
        run_ingest(connection, config, mailbox, clock)
        jobs_before = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        clock.advance(hours=6)
        second = run_ingest(connection, config, mailbox, clock)
        assert second.messages_listed == 3 and second.messages_new == 0 and second.jobs_parsed == 0
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs_before
        assert mailbox.fetched.count("m-linkedin") == 1                       # the body is fetched once, ever
        rows = connection.execute("SELECT * FROM processed_emails ORDER BY gmail_message_id").fetchall()
        assert [(r["gmail_message_id"], r["parsed_job_count"]) for r in rows] == [("m-intern", 2), ("m-linkedin", 3), ("m-naukri", 2)]

    def test_job_resent_in_a_later_email_updates_instead_of_duplicating(self, connection, config, clock: FakeClock) -> None:
        first = FakeMailbox([raw("a", "jobalerts-noreply@linkedin.com", "linkedin", RECEIVED)])
        run_ingest(connection, config, first, clock)
        second = FakeMailbox([raw("b", "jobalerts-noreply@linkedin.com", "linkedin", RECEIVED + timedelta(days=3))])
        summary = run_ingest(connection, config, second, clock)
        assert summary.jobs_new == 0 and summary.messages_new == 1
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM job_sightings").fetchone()[0] == 3

    def test_unlisted_sender_is_recorded_but_never_parsed(self, connection, config, clock) -> None:
        box = FakeMailbox([raw("evil", "alerts@linkedin.com.evil.example", "linkedin"), raw("friend", "friend@gmail.com", "linkedin")])
        summary = run_ingest(connection, config, box, clock)
        assert (summary.messages_skipped_sender, summary.jobs_parsed) == (2, 0)
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM processed_emails").fetchone()[0] == 2

    def test_disabled_platform_is_skipped(self, connection, config, clock) -> None:
        import dataclasses

        platforms = {**config.email.platforms, "linkedin": dataclasses.replace(config.email.platforms["linkedin"], enabled=False)}
        tweaked = dataclasses.replace(config, email=dataclasses.replace(config.email, platforms=platforms))
        box = FakeMailbox([raw("m", "jobalerts-noreply@linkedin.com", "linkedin")])
        assert run_ingest(connection, tweaked, box, clock).jobs_parsed == 0
        assert "linkedin.com" not in build_query(tweaked, RECEIVED)

    def test_prefilter_applies_to_email_jobs(self, connection, config, clock) -> None:
        html = ('<div><a href="https://www.linkedin.com/jobs/view/3911111111">Senior Staff Engineer</a><p>Acme · Pune, India</p></div>'
                '<div><a href="https://www.linkedin.com/jobs/view/3922222222">Sales Executive</a><p>Acme · Pune, India</p></div>'
                '<div><a href="https://www.linkedin.com/jobs/view/3933333333">Software Engineer</a><p>Acme · Pune, India</p></div>')
        box = FakeMailbox([RawEmail("m", "jobalerts-noreply@linkedin.com", RECEIVED, html, "")])
        summary = run_ingest(connection, config, box, clock)
        assert (summary.jobs_parsed, summary.jobs_new, summary.jobs_skipped_by_prefilter) == (3, 1, 2)

    def test_parser_crash_leaves_the_message_unprocessed_for_retry(self, connection, config, clock, monkeypatch) -> None:
        import jobhunt.alerts.pipeline as pipeline

        def boom(*args, **kwargs):
            raise RuntimeError("parser bug")

        monkeypatch.setattr(pipeline, "parse_email", boom)
        box = FakeMailbox([raw("m", "jobalerts-noreply@linkedin.com", "linkedin")])
        summary = run_ingest(connection, config, box, clock)
        assert summary.errors and connection.execute("SELECT COUNT(*) FROM processed_emails").fetchone()[0] == 0

    def test_first_run_looks_back_then_continues_from_the_stored_position(self, connection, config, mailbox, clock: FakeClock) -> None:
        run_ingest(connection, config, mailbox, clock)
        first_query = mailbox.queries[0]
        assert f"after:{int((clock() - timedelta(days=14)).timestamp())}" in first_query
        clock.advance(days=1)
        run_ingest(connection, config, mailbox, clock)
        newest = int(RECEIVED.timestamp())
        assert f"after:{newest - 86400}" in mailbox.queries[1]            # newest seen minus one day of overlap
        assert connection.execute("SELECT cursor_or_etag FROM source_state WHERE source = 'gmail'").fetchone()[0] == str(newest)

    def test_since_days_overrides_the_stored_position(self, connection, config, mailbox, clock: FakeClock) -> None:
        run_ingest(connection, config, mailbox, clock)
        run_ingest(connection, config, mailbox, clock, since_days=3)
        assert f"after:{int((clock() - timedelta(days=3)).timestamp())}" in mailbox.queries[1]

    def test_old_email_jobs_age_out(self, connection, config, clock: FakeClock) -> None:
        run_ingest(connection, config, FakeMailbox([raw("a", "jobalerts-noreply@linkedin.com", "linkedin")]), clock)
        clock.advance(days=45)
        summary = run_ingest(connection, config, FakeMailbox([]), clock)
        assert summary.jobs_closed == 3
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE status = 'closed'").fetchone()[0] == 3

    def test_limit_caps_messages(self, connection, config, mailbox, clock) -> None:
        summary = run_ingest(connection, config, mailbox, clock, limit=1)
        assert summary.messages_listed == 1


class TestAtsLinkResolution:
    BOARD = r"https://boards-api\.greenhouse\.io/v1/boards/rocketai/jobs.*"

    @pytest.fixture()
    def ats_email(self) -> FakeMailbox:
        return FakeMailbox([raw("ats", "team@wellfound.com", "wellfound_ats_links")])

    def board_payload(self) -> dict:
        return {"jobs": [{
            "id": 4455667, "title": "Software Engineer, Platform", "company_name": "Rocket AI",
            "absolute_url": "https://boards.greenhouse.io/rocketai/jobs/4455667", "location": {"name": "Remote - US"},
            "content": "&lt;p&gt;Build the platform. Full description from the ATS API.&lt;/p&gt;",
            "first_published": "2026-10-01T10:00:00Z",
        }]}

    def test_ats_company_is_discovered_probed_and_the_posting_fetched_via_its_api(
        self, connection, config, ats_email, clock, http_client, router
    ) -> None:
        board = router.get(url__regex=self.BOARD).mock(return_value=httpx.Response(200, json=self.board_payload()))
        lever = router.get(url__regex=r"https://api\.(eu\.)?lever\.co/v0/postings/globexlabs.*").mock(
            return_value=httpx.Response(404, json={"ok": False}))
        summary = run_ingest(connection, config, ats_email, clock, http=http_client, ats_sources=build_ats_sources(config))
        assert summary.companies_discovered == 2 and summary.ats_companies_resolved == 2

        companies = {r["slug"]: r for r in connection.execute("SELECT * FROM companies")}
        assert companies["rocketai"]["status"] == "active" and companies["rocketai"]["discovered_via"] == "email:wellfound"
        assert companies["globexlabs"]["status"] == "unverified" and companies["globexlabs"]["consecutive_failures"] == 1

        job = connection.execute(
            "SELECT * FROM jobs WHERE canonical_apply_url = 'https://boards.greenhouse.io/rocketai/jobs/4455667'").fetchone()
        assert job["needs_description"] == 0 and "Full description from the ATS API" in job["description_text"]
        sources = {r["source"] for r in connection.execute(
            "SELECT source FROM job_sightings WHERE job_id = ?", (job["id"],))}
        assert sources == {"email_wellfound", "greenhouse"}                      # one job, two sightings
        assert board.call_count >= 2                                             # probe + ingest
        assert lever.call_count >= 1
        hosts = {call.request.url.host for call in router.calls}
        assert hosts <= {"boards-api.greenhouse.io", "api.lever.co", "api.eu.lever.co"}   # ATS APIs only

    def test_non_ats_links_in_the_same_email_are_never_requested(self, connection, config, ats_email, clock, http_client, router) -> None:
        router.get(url__regex=self.BOARD).mock(return_value=httpx.Response(200, json=self.board_payload()))
        router.get(url__regex=r"https://api\.(eu\.)?lever\.co/.*").mock(return_value=httpx.Response(404, json={}))
        run_ingest(connection, config, ats_email, clock, http=http_client, ats_sources=build_ats_sources(config))
        assert all("wellfound.com" not in str(call.request.url) for call in router.calls)
        native = connection.execute("SELECT * FROM jobs WHERE canonical_apply_url = 'https://wellfound.com/jobs/3456789-backend-engineer-new-grad'").fetchone()
        assert native is not None and native["needs_description"] == 1

    def test_resolution_can_be_switched_off(self, connection, config, ats_email, clock, http_client, router) -> None:
        summary = run_ingest(connection, config, ats_email, clock, http=http_client,
                             ats_sources=build_ats_sources(config), resolve_ats=False)
        assert summary.companies_discovered == 2 and summary.ats_companies_resolved == 0
        assert router.calls.call_count == 0
        assert connection.execute("SELECT status FROM companies WHERE slug = 'rocketai'").fetchone()[0] == "unverified"

    def test_without_an_http_client_nothing_is_fetched(self, connection, config, ats_email, clock, router) -> None:
        summary = run_ingest(connection, config, ats_email, clock)
        assert summary.ats_companies_resolved == 0 and router.calls.call_count == 0


# --- CLI ----------------------------------------------------------------------------------------

class TestCli:
    def test_parse_email_html(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["--config", str(FIXTURE_DIR.parent.parent / "config.yaml"), "parse-email",
                     str(EMAIL_DIR / "naukri.html"), "--platform", "naukri"])
        out = capsys.readouterr().out
        assert code == 0 and "2 job card(s)" in out and "Software Engineer I | Acme Technologies | Pune, Maharashtra" in out

    def test_parse_email_eml(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        eml = (
            "From: Job Alerts <jobalerts-noreply@linkedin.com>\r\nTo: me@example.com\r\nSubject: alert\r\n"
            "Date: Mon, 12 Oct 2026 06:30:00 +0000\r\nMIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            + fixture_html("linkedin")
        )
        path = tmp_path / "sample.eml"
        path.write_bytes(eml.encode("utf-8"))   # bytes: write_text would double the CRLFs on Windows
        message = message_from_eml(path.read_bytes())
        assert message.sender == "jobalerts-noreply@linkedin.com" and message.received_at == RECEIVED
        code = main(["--config", str(FIXTURE_DIR.parent.parent / "config.yaml"), "parse-email", str(path), "--platform", "linkedin"])
        assert code == 0 and "3 job card(s)" in capsys.readouterr().out

    def test_unknown_platform_is_a_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["--config", str(FIXTURE_DIR.parent.parent / "config.yaml"), "parse-email",
                     str(EMAIL_DIR / "naukri.html"), "--platform", "monster"])
        assert code == 2

    def test_gmail_auth_without_credentials_gives_a_friendly_error(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("email:\n  credentials_file: missing/credentials.json\n  token_file: missing/token.json\n", encoding="utf-8")
        assert main(["--config", str(config_file), "gmail-auth"]) == 2

    def test_ingest_email_without_a_token_asks_for_auth(self, tmp_path: Path, capsys, caplog) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            f"database_path: {(tmp_path / 'db.sqlite3').as_posix()}\nmigrations_dir: {(FIXTURE_DIR.parent.parent / 'migrations').as_posix()}\n"
            "email:\n  token_file: nope/token.json\n", encoding="utf-8")
        assert main(["--config", str(config_file), "ingest-email"]) == 2
        assert any("gmail-auth" in record.getMessage() for record in caplog.records)
