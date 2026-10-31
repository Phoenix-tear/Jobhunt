"""Embeddings (mocked API): one vector per input, caching, re-embedding rules, PII, budget, similarity."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import sqlite3
from typing import Any

import httpx
import pytest
import respx

from jobhunt.config import BudgetSettings, Config, EmbeddingSettings
from jobhunt.matching.budget import BudgetGuard, UsageLedger
from jobhunt.matching.embeddings import (
    collect_pending,
    compute_similarity,
    embed_jobs,
    embed_profiles,
    format_job_text,
    format_profile_text,
    truncate_description,
    unpack,
)
from jobhunt.matching.gate import run_relevance
from jobhunt.matching.gemini import GeminiAuthError, GeminiClient, GeminiResponseError
from jobhunt.matching.profiles import PiiResult  # noqa: F401  (import check: public surface)

DIMS = 8
EMBED_URL = r"https://generativelanguage\.googleapis\.com/v1beta/models/[\w.-]+:batchEmbedContents"


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def fake_vector(text: str, *, unit: bool = True, dims: int = DIMS) -> list[float]:
    """Deterministic: ML-ish text points along axis 0, web-ish along axis 1, everything else is noise."""
    lowered = text.lower()
    vector = [0.0] * dims
    if "machine learning" in lowered or "llm" in lowered:
        vector[0] = 1.0
    if "react" in lowered or "frontend" in lowered:
        vector[1] = 1.0
    if sum(vector) == 0:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vector = [0.0, 0.0] + [byte / 255 + 0.1 for byte in digest[: dims - 2]]   # off the ML / web axes
    return _unit(vector) if unit else [v * 0.6 for v in _unit(vector)]


class FakeGemini:
    """respx side effect for batchEmbedContents; records every request body."""

    def __init__(self, *, unit: bool = True, fail_on: str | None = None, dims: int = DIMS) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.unit = unit
        self.fail_on = fail_on
        self.dims = dims

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        texts = [item["content"]["parts"][0]["text"] for item in body["requests"]]
        if self.fail_on and any(self.fail_on in text for text in texts):
            return httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT", "message": "bad text"}})
        return httpx.Response(200, json={
            "embeddings": [{"values": fake_vector(text, unit=self.unit, dims=self.dims)} for text in texts],
            "usageMetadata": {"promptTokenCount": sum(len(text) // 4 + 1 for text in texts)},
        })

    @property
    def requests(self) -> list[dict[str, Any]]:
        return [item for body in self.bodies for item in body["requests"]]


@pytest.fixture()
def fake(router: respx.MockRouter) -> FakeGemini:
    service = FakeGemini()
    router.post(url__regex=EMBED_URL).mock(side_effect=service)
    return service


@pytest.fixture()
def cfg(config: Config) -> Config:
    return dataclasses.replace(
        config,
        embedding=EmbeddingSettings(model="gemini-embedding-2", dimensions=DIMS, batch_size=2, max_concurrent_batches=2,
                                    description_token_limit=50),
        budget=BudgetSettings(daily_calls=100, monthly_usd=5.0, daily_embedding_texts=1000),
    )


@pytest.fixture()
def client(cfg: Config) -> GeminiClient:
    sleeps: list[float] = []
    instance = GeminiClient("test-key", cfg.gemini, sleep=sleeps.append, jitter=lambda: 0.0)
    instance.slept = sleeps  # type: ignore[attr-defined]
    return instance


def services(connection: sqlite3.Connection, cfg: Config) -> tuple[UsageLedger, BudgetGuard]:
    ledger = UsageLedger(connection, cfg.pricing)
    return ledger, BudgetGuard(ledger, cfg.budget)


def add_job(connection: sqlite3.Connection, n: int, title: str, description: str | None = "Python APIs.", **values: Any) -> int:
    row: dict[str, Any] = {
        "fingerprint": f"fp{n}", "title": title, "company_name": "Acme", "location_raw": "Pune, India",
        "first_seen_at": f"2026-10-{10 + n % 5:02d}T00:00:00Z", "last_seen_at": "2026-10-12T00:00:00Z",
        "status": "open", "description_text": description, "description_hash": f"h{n}",
    }
    row.update(values)
    cursor = connection.execute(
        f"INSERT INTO jobs ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values())
    )
    return int(cursor.lastrowid)


def seed(connection: sqlite3.Connection, cfg: Config, clock) -> dict[str, int]:
    ids = {
        "ml": add_job(connection, 1, "Machine Learning Engineer", "Build LLM agents with machine learning."),
        "web": add_job(connection, 2, "Frontend Engineer", "React and TypeScript frontend work."),
        "se": add_job(connection, 3, "Software Engineer", "Backend services in Python."),
        "java": add_job(connection, 4, "Java Developer", "Spring Boot."),                # excluded by the gate
        "closed": add_job(connection, 5, "Software Engineer", "Old.", status="closed"),
        "salesforce": add_job(connection, 6, "Salesforce Administrator", "CRM work."),     # maybe
    }
    run_relevance(connection, cfg, clock=clock)
    return ids


def embedded_ids(connection: sqlite3.Connection) -> set[int]:
    return {row["job_id"] for row in connection.execute("SELECT job_id FROM job_embeddings")}


# --- the Embedding-2 aggregation gotcha -------------------------------------------------------


def test_n_inputs_give_n_distinct_vectors_in_order(fake: FakeGemini, client: GeminiClient) -> None:
    texts = ["machine learning role", "react frontend role", "barista role", "chef role", "x"]
    result = client.embed_batch("gemini-embedding-2", texts, dimensions=DIMS)
    assert len(result.vectors) == 5
    assert len({tuple(round(v, 6) for v in vector) for vector in result.vectors}) == 5      # one distinct vector each
    assert [result.vectors[i] for i in range(5)] == [fake_vector(text) for text in texts]  # in input order
    # exactly one request item per input, each with ONE content holding ONE part (never merged parts)
    assert len(fake.requests) == 5
    assert all(len(item["content"]["parts"]) == 1 for item in fake.requests)
    assert [item["content"]["parts"][0]["text"] for item in fake.requests] == texts
    assert all(item["outputDimensionality"] == DIMS for item in fake.requests)
    assert all("taskType" not in item for item in fake.requests)    # embedding-2 has no task_type


def test_task_type_is_sent_for_embedding_001(router: respx.MockRouter, cfg: Config) -> None:
    service = FakeGemini(unit=False)
    router.post(url__regex=EMBED_URL).mock(side_effect=service)
    with GeminiClient("k", cfg.gemini, sleep=lambda s: None) as client:
        client.embed_batch("gemini-embedding-001", ["a", "b"], dimensions=DIMS, task_type="RETRIEVAL_DOCUMENT")
    assert {item["taskType"] for item in service.requests} == {"RETRIEVAL_DOCUMENT"}
    assert all(item["model"] == "models/gemini-embedding-001" for item in service.requests)


def test_a_wrong_count_or_dimension_is_an_error(router: respx.MockRouter, cfg: Config) -> None:
    router.post(url__regex=EMBED_URL).mock(return_value=httpx.Response(200, json={"embeddings": [{"values": [0.1] * DIMS}]}))
    with GeminiClient("k", cfg.gemini, sleep=lambda s: None) as client:
        with pytest.raises(GeminiResponseError, match="expected 2 embeddings"):
            client.embed_batch("gemini-embedding-2", ["a", "b"], dimensions=DIMS)
    router.post(url__regex=EMBED_URL).mock(return_value=httpx.Response(200, json={"embeddings": [{"values": [0.1] * 3}]}))
    with GeminiClient("k", cfg.gemini, sleep=lambda s: None) as client:
        with pytest.raises(GeminiResponseError, match="expected 8"):
            client.embed_batch("gemini-embedding-2", ["a"], dimensions=DIMS)


def test_missing_key_is_refused_before_any_request(cfg: Config) -> None:
    with pytest.raises(GeminiAuthError):
        GeminiClient("  ", cfg.gemini)


# --- text formats -----------------------------------------------------------------------------


def test_prompt_formats_for_each_model() -> None:
    profile, task = format_profile_text("I build agents.", "gemini-embedding-2")
    assert profile == "task: search result | query: I build agents." and task is None
    job, task = format_job_text("ML Engineer", "Acme", "Pune", "Python and LLMs.", "gemini-embedding-2", 1500)
    assert job == "title: ML Engineer | text: Acme | Pune | Python and LLMs." and task is None
    old_job, old_task = format_job_text("ML Engineer", "Acme", None, None, "gemini-embedding-001", 1500)
    assert old_job == "ML Engineer\nAcme" and old_task == "RETRIEVAL_DOCUMENT"
    assert format_profile_text("x", "gemini-embedding-001") == ("x", "RETRIEVAL_QUERY")


def test_description_is_capped_at_the_token_limit() -> None:
    long_text = "word " * 5000
    capped = truncate_description(long_text, 100)
    assert len(capped) <= 400 and not capped.endswith(" ")
    assert truncate_description("short text", 100) == "short text"
    assert truncate_description(None, 100) == ""


# --- the pipeline -----------------------------------------------------------------------------


def test_only_open_target_and_maybe_jobs_are_embedded(
    connection: sqlite3.Connection, cfg: Config, clock, fake: FakeGemini, client: GeminiClient
) -> None:
    ids = seed(connection, cfg, clock)
    ledger, guard = services(connection, cfg)
    stats = embed_jobs(connection, cfg, client, ledger, guard, clock=clock)
    assert embedded_ids(connection) == {ids["ml"], ids["web"], ids["se"], ids["salesforce"]}
    assert stats.embedded == 4 and stats.failed == 0 and stats.batches == 2
    assert ids["java"] not in embedded_ids(connection) and ids["closed"] not in embedded_ids(connection)
    row = connection.execute("SELECT * FROM job_embeddings WHERE job_id = ?", (ids["ml"],)).fetchone()
    assert (row["model"], row["dimensions"], row["prompt_version"]) == ("gemini-embedding-2", DIMS, "v1")
    assert len(unpack(row["vector"])) == DIMS and len(row["text_hash"]) == 64
    assert connection.execute("SELECT embedded_at FROM jobs WHERE id = ?", (ids["ml"],)).fetchone()[0] is not None
    usage = ledger.summary()
    assert usage.embed_texts_today == 4 and usage.usd_month > 0


def test_second_run_is_all_cache_hits(
    connection: sqlite3.Connection, cfg: Config, clock, fake: FakeGemini, client: GeminiClient
) -> None:
    seed(connection, cfg, clock)
    ledger, guard = services(connection, cfg)
    embed_jobs(connection, cfg, client, ledger, guard, clock=clock)
    calls = len(fake.bodies)
    again = embed_jobs(connection, cfg, client, ledger, guard, clock=clock)
    assert again.embedded == 0 and again.cached == 4 and len(fake.bodies) == calls


@pytest.mark.parametrize("change", ["description", "model", "dimensions", "prompt_version"])
def test_reembed_only_when_hash_or_settings_change(
    connection: sqlite3.Connection, cfg: Config, clock, router: respx.MockRouter, change: str
) -> None:
    service = FakeGemini()
    router.post(url__regex=EMBED_URL).mock(side_effect=service)
    ids = seed(connection, cfg, clock)
    ledger, guard = services(connection, cfg)
    with GeminiClient("k", cfg.gemini, sleep=lambda s: None) as client:
        embed_jobs(connection, cfg, client, ledger, guard, clock=clock)
        before = len(service.requests)

        new_cfg = cfg
        if change == "description":
            connection.execute("UPDATE jobs SET description_text = 'Totally different text.' WHERE id = ?", (ids["se"],))
            expected = 1
        elif change == "model":
            new_cfg = dataclasses.replace(cfg, embedding=dataclasses.replace(cfg.embedding, model="gemini-embedding-001"))
            expected = 4
        elif change == "dimensions":
            new_cfg = dataclasses.replace(cfg, embedding=dataclasses.replace(cfg.embedding, dimensions=4))
            service.dims = 4
            expected = 4
        else:
            new_cfg = dataclasses.replace(cfg, embedding=dataclasses.replace(cfg.embedding, prompt_version="v2"))
            expected = 4
        embed_jobs(connection, new_cfg, client, ledger, guard, clock=clock)
    assert len(service.requests) - before == expected


def test_limit_caps_new_texts_and_targets_go_first(
    connection: sqlite3.Connection, cfg: Config, clock, fake: FakeGemini, client: GeminiClient
) -> None:
    ids = seed(connection, cfg, clock)
    ledger, guard = services(connection, cfg)
    stats = embed_jobs(connection, cfg, client, ledger, guard, limit=3, clock=clock)
    assert stats.embedded == 3
    assert ids["salesforce"] not in embedded_ids(connection)      # the lone `maybe` waits for the targets


def test_dry_run_style_collection_makes_no_request(
    connection: sqlite3.Connection, cfg: Config, clock, router: respx.MockRouter
) -> None:
    seed(connection, cfg, clock)
    pending, stats = collect_pending(connection, cfg)
    assert stats.to_embed == 4 and stats.tokens > 0 and len(pending) == 4
    assert router.calls.call_count == 0


def test_one_failing_batch_does_not_lose_the_others(
    connection: sqlite3.Connection, cfg: Config, clock, router: respx.MockRouter
) -> None:
    service = FakeGemini(fail_on="Backend services")
    router.post(url__regex=EMBED_URL).mock(side_effect=service)
    ids = seed(connection, cfg, clock)
    ledger, guard = services(connection, cfg)
    with GeminiClient("k", cfg.gemini, sleep=lambda s: None) as client:
        stats = embed_jobs(connection, cfg, client, ledger, guard, clock=clock)
    assert stats.failed >= 1 and stats.embedded >= 1
    assert ids["se"] not in embedded_ids(connection)
    assert stats.failed + stats.embedded == 4 and stats.errors


def test_rate_limit_retry_after_is_honored_then_succeeds(
    connection: sqlite3.Connection, cfg: Config, clock, router: respx.MockRouter
) -> None:
    ids = seed(connection, cfg, clock)
    service = FakeGemini()
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"status": "RESOURCE_EXHAUSTED"}})])

    def flaky(request: httpx.Request) -> httpx.Response:
        first = next(responses, None)
        return first if first is not None else service(request)

    router.post(url__regex=EMBED_URL).mock(side_effect=flaky)
    sleeps: list[float] = []
    ledger, guard = services(connection, cfg)
    with GeminiClient("k", cfg.gemini, sleep=sleeps.append, jitter=lambda: 0.0) as client:
        stats = embed_jobs(connection, dataclasses.replace(cfg, embedding=dataclasses.replace(cfg.embedding, max_concurrent_batches=1)),
                           client, ledger, guard, clock=clock)
    assert stats.failed == 0 and stats.embedded == 4
    assert sleeps and max(sleeps) >= 7.0                      # Retry-After respected
    assert ids["ml"] in embedded_ids(connection)


def test_budget_cap_stops_embedding(
    connection: sqlite3.Connection, cfg: Config, clock, fake: FakeGemini, client: GeminiClient
) -> None:
    seed(connection, cfg, clock)
    tight = dataclasses.replace(cfg, budget=BudgetSettings(daily_calls=100, monthly_usd=5.0, daily_embedding_texts=2))
    ledger, guard = services(connection, tight)
    stats = embed_jobs(connection, tight, client, ledger, guard, clock=clock)
    assert stats.embedded == 0 and "daily embedding cap" in (stats.stopped_reason or "")
    assert fake.bodies == []                                   # no call was made past the cap

    broke = dataclasses.replace(cfg, budget=BudgetSettings(daily_calls=100, monthly_usd=0.0, daily_embedding_texts=1000))
    ledger, guard = services(connection, broke)
    stats = embed_jobs(connection, broke, client, ledger, guard, clock=clock)
    assert "monthly spend cap" in (stats.stopped_reason or "") and fake.bodies == []


# --- profiles: nothing personal leaves the machine ----------------------------------------------


def test_profile_embedding_request_contains_no_pii(
    connection: sqlite3.Connection, cfg: Config, clock, fake: FakeGemini, client: GeminiClient
) -> None:
    from jobhunt.matching.profiles import strip_pii

    raw = ("JANE DOE\njane.doe@example.com | +91 98765 43210 | linkedin.com/in/janedoe\n"
           "SUMMARY\nI build machine learning systems and ship React frontends for startups across India and beyond. " * 3)
    stripped = strip_pii(raw).text
    for name in ("ai", "fullstack"):
        connection.execute(
            """INSERT INTO profiles (name, source_file, source_hash, text_hash, stripped_text, built_at)
               VALUES (?, 'r.pdf', 'h', 'th', ?, '2026-10-12T00:00:00Z')""", (name, stripped))
    ledger, guard = services(connection, cfg)
    assert embed_profiles(connection, cfg, client, ledger, guard, clock=clock) == 2
    payload = json.dumps(fake.bodies)
    for secret in ("jane.doe@example.com", "98765", "linkedin", "JANE", "Jane", "janedoe"):
        assert secret not in payload
    assert "task: search result | query: SUMMARY" in payload
    assert embed_profiles(connection, cfg, client, ledger, guard, clock=clock) == 0       # cached


# --- similarity -------------------------------------------------------------------------------


def test_similarity_picks_the_better_profile_and_ranks(
    connection: sqlite3.Connection, cfg: Config, clock, fake: FakeGemini, client: GeminiClient
) -> None:
    ids = seed(connection, cfg, clock)
    connection.execute(
        "INSERT INTO profiles (name, source_file, source_hash, text_hash, stripped_text, built_at) VALUES "
        "('ai', 'a.pdf', 'h', 't1', 'I build machine learning LLM systems', '2026-10-12T00:00:00Z'),"
        "('fullstack', 'f.pdf', 'h', 't2', 'I build React frontend websites', '2026-10-12T00:00:00Z')"
    )
    ledger, guard = services(connection, cfg)
    embed_profiles(connection, cfg, client, ledger, guard, clock=clock)
    embed_jobs(connection, cfg, client, ledger, guard, clock=clock)

    stats = compute_similarity(connection, cfg, clock=clock)
    assert stats.jobs_scored == 4 and stats.profiles == ["ai", "fullstack"]
    rows = {row["job_id"]: row for row in connection.execute("SELECT * FROM job_relevance")}
    assert rows[ids["ml"]]["best_profile"] == "ai"
    assert rows[ids["web"]]["best_profile"] == "fullstack"
    assert rows[ids["ml"]]["similarity_ai"] > rows[ids["ml"]]["similarity_fullstack"]
    assert rows[ids["java"]]["similarity_ai"] is None            # never embedded
    percentiles = [rows[i]["similarity_percentile"] for i in (ids["ml"], ids["web"], ids["se"], ids["salesforce"])]
    assert min(percentiles) == 0.0 and max(percentiles) == 1.0
    assert all(0.99 <= math.sqrt(sum(v * v for v in unpack(r["vector"]))) <= 1.01
               for r in connection.execute("SELECT vector FROM job_embeddings"))


# --- pacing ---------------------------------------------------------------------------------------


def test_token_pacer_waits_for_the_minute_window_to_open() -> None:
    from jobhunt.matching.embeddings import TokenPacer

    now = [1000.0]
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    pacer = TokenPacer(100, sleep=fake_sleep, monotonic=lambda: now[0])
    assert pacer.wait_for(60) == 0.0          # fits
    assert pacer.wait_for(30) == 0.0          # still fits (90 of 100)
    waited = pacer.wait_for(40)               # 130 > 100: must wait for the first event to leave the window
    assert waited > 0 and abs(sum(slept) - 60.05) < 0.1
    assert pacer.wait_for(10_000) >= 0.0      # a single oversized wave still runs (capped to the limit)
