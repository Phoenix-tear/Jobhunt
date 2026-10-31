"""HTTP client: retries, Retry-After, timeouts, per-host spacing. No real sleeping or network."""

from __future__ import annotations

import dataclasses

import httpx
import pytest
import respx

from jobhunt.config import Config
from jobhunt.errors import TransientFetchError
from jobhunt.http import HttpClient, parse_retry_after

URL = "https://example.test/api/jobs"


def test_success_is_returned_without_retry(router: respx.MockRouter, http_client: HttpClient, sleeps: list[float]) -> None:
    route = router.get(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert http_client.get(URL).json() == {"ok": True}
    assert route.call_count == 1 and sleeps == []


def test_client_errors_are_returned_not_retried(router: respx.MockRouter, http_client: HttpClient) -> None:
    route = router.get(URL).mock(return_value=httpx.Response(404))
    assert http_client.get(URL).status_code == 404
    assert route.call_count == 1


def test_5xx_then_success(router: respx.MockRouter, http_client: HttpClient, sleeps: list[float]) -> None:
    route = router.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"n": 1})])
    assert http_client.get(URL).json() == {"n": 1}
    assert route.call_count == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] > 0     # exponential backoff


def test_exhausted_retries_raise_transient_error(router: respx.MockRouter, http_client: HttpClient) -> None:
    route = router.get(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(TransientFetchError, match="HTTP 500 after 3 attempts"):
        http_client.get(URL)
    assert route.call_count == 3  # first try + max_retries(2)


def test_429_honors_retry_after(router: respx.MockRouter, http_client: HttpClient, sleeps: list[float]) -> None:
    router.get(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})])
    http_client.get(URL)
    assert max(sleeps) >= 7


def test_retry_after_beyond_cap_gives_up_immediately(router: respx.MockRouter, http_client: HttpClient) -> None:
    route = router.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "86400"}))
    with pytest.raises(TransientFetchError, match="exceeds cap"):
        http_client.get(URL)
    assert route.call_count == 1


def test_timeouts_are_retried_then_reported(router: respx.MockRouter, http_client: HttpClient) -> None:
    route = router.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(TransientFetchError, match="timeout"):
        http_client.get(URL)
    assert route.call_count == 3


def test_network_error_then_success(router: respx.MockRouter, http_client: HttpClient) -> None:
    router.get(URL).mock(side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json={"a": 1})])
    assert http_client.get(URL).json() == {"a": 1}


def test_redirects_are_not_followed(router: respx.MockRouter, http_client: HttpClient) -> None:
    router.get(URL).mock(return_value=httpx.Response(302, headers={"Location": "https://elsewhere.test/"}))
    assert http_client.get(URL).status_code == 302


def test_user_agent_header_is_sent(router: respx.MockRouter, http_client: HttpClient) -> None:
    route = router.get(URL).mock(return_value=httpx.Response(200, json={}))
    http_client.get(URL)
    assert route.calls.last.request.headers["user-agent"] == "jobhunt-tests/0"


def test_per_host_spacing_is_enforced(router: respx.MockRouter, config: Config) -> None:
    ticks = {"now": 100.0}
    sleeps: list[float] = []
    settings = dataclasses.replace(config.http, default_min_interval_seconds=1.0, host_min_interval_seconds={})
    client = HttpClient(settings, "ua", sleep=sleeps.append, monotonic=lambda: ticks["now"])
    router.get(url__regex=r"https://example\.test/.*").mock(return_value=httpx.Response(200, json={}))
    client.get("https://example.test/a")
    client.get("https://example.test/b")   # same host, same instant -> must wait the interval
    assert sleeps == [1.0]
    client.close()


def test_host_specific_interval_overrides_default(router: respx.MockRouter, config: Config) -> None:
    sleeps: list[float] = []
    settings = dataclasses.replace(
        config.http, default_min_interval_seconds=0.0, host_min_interval_seconds={"slow.test": 2.5}
    )
    client = HttpClient(settings, "ua", sleep=sleeps.append, monotonic=lambda: 50.0)
    router.get(url__regex=r"https://.*").mock(return_value=httpx.Response(200, json={}))
    client.get("https://slow.test/a")
    client.get("https://slow.test/b")
    client.get("https://fast.test/a")
    client.get("https://fast.test/b")
    assert sleeps == [2.5]
    client.close()


def test_query_string_never_appears_in_error_messages(router: respx.MockRouter, http_client: HttpClient) -> None:
    router.get(url__regex=r"https://example\.test/.*").mock(return_value=httpx.Response(500))
    with pytest.raises(TransientFetchError) as error:
        http_client.get(URL, params={"app_key": "SECRET123"})
    assert "SECRET123" not in str(error.value)


def test_request_count_includes_retries(router: respx.MockRouter, http_client: HttpClient) -> None:
    router.get(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200, json={})])
    http_client.get(URL)
    assert http_client.request_count == 2


@pytest.mark.parametrize(
    ("header", "expected"),
    [("5", 5.0), ("0", 0.0), ("-3", 0.0), ("1.5", 1.5), (None, None), ("", None), ("soon", None)],
)
def test_parse_retry_after_seconds(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header) == expected


def test_parse_retry_after_http_date_in_the_past_is_zero() -> None:
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
