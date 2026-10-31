"""Gemini Batch API scoring (mocked): submit, collect, price factor, failures, nothing scored twice."""

from __future__ import annotations

import dataclasses
import json
import sqlite3

import httpx
import pytest
import respx

from jobhunt.config import Config
from jobhunt.matching.scoring import collect_score_batch, submit_score_batch
from tests.test_scoring import cfg, make_client, row, seed, services  # noqa: F401  (cfg is a fixture)

BATCH_URL = r"https://generativelanguage\.googleapis\.com/v1beta/models/[\w.-]+:batchGenerateContent"
GOOD = {"fit_score": 74, "matched_skills": ["Python"], "missing_skills": [], "red_flags": [],
        "is_excluded_role": False, "recommended_profile": "ai", "reason": "Solid fit."}


def reply(key: str, payload: dict, tokens_in: int = 1000, tokens_out: int = 100) -> dict:
    return {"metadata": {"key": key}, "response": {
        "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": tokens_in, "candidatesTokenCount": tokens_out}}}


@pytest.fixture()
def bcfg(cfg: Config) -> Config:  # noqa: F811
    return dataclasses.replace(cfg, scoring=dataclasses.replace(cfg.scoring, prompt_style="compact"))


def test_submit_then_collect_applies_scores_at_half_price(
    connection: sqlite3.Connection, bcfg: Config, clock, router: respx.MockRouter  # noqa: F811
) -> None:
    ids = seed(connection, bcfg, clock, [("Machine Learning Engineer", "Python agents.", {}),
                                         ("Python Developer", "FastAPI services.", {})])
    created = router.post(url__regex=BATCH_URL).mock(return_value=httpx.Response(200, json={"name": "batches/abc"}))
    ledger, guard = services(connection, bcfg)
    with make_client(bcfg) as client:
        submitted = submit_score_batch(connection, bcfg, client, ledger, guard, k=5, clock=clock)
    assert submitted.submitted == 2 and submitted.name == "batches/abc"
    sent = json.loads(created.calls[0].request.content)["batch"]["input_config"]["requests"]["requests"]
    assert {item["metadata"]["key"] for item in sent} == {f"job-{i}" for i in ids}
    assert "systemInstruction" in sent[0]["request"] and "generationConfig" in sent[0]["request"]

    # a second submit while the first is in flight sends nothing new
    with make_client(bcfg) as client:
        again = submit_score_batch(connection, bcfg, client, ledger, guard, k=5, clock=clock)
    assert again.submitted == 0

    router.get("https://generativelanguage.googleapis.com/v1beta/batches/abc").mock(return_value=httpx.Response(
        200, json={"name": "batches/abc", "done": True, "metadata": {"state": "BATCH_STATE_SUCCEEDED"},
                   "response": {"inlinedResponses": [reply(f"job-{ids[0]}", GOOD),
                                                      reply(f"job-{ids[1]}", {"bad": "shape"})]}}))
    with make_client(bcfg) as client:
        collected = collect_score_batch(connection, bcfg, client, ledger, clock=clock)
    assert collected.collected == 1 and collected.failed == 1
    assert row(connection, ids[0])["llm_status"] == "scored" and row(connection, ids[0])["llm_score"] == 74
    assert row(connection, ids[1])["llm_status"] == "failed"
    usd = connection.execute("SELECT estimated_usd, input_tokens FROM llm_usage WHERE purpose = 'score' "
                             "ORDER BY id LIMIT 1").fetchone()
    full_price = (1000 * 0.75 + 100 * 3.75) / 1_000_000          # gemini-3.8-flash list price
    assert usd["estimated_usd"] == pytest.approx(full_price * 0.5, rel=0.01)
    assert connection.execute("SELECT status FROM llm_batches").fetchone()["status"] == "applied"


def test_unfinished_batch_stays_pending(connection: sqlite3.Connection, bcfg: Config, clock,  # noqa: F811
                                        router: respx.MockRouter) -> None:
    seed(connection, bcfg, clock, [("Machine Learning Engineer", "Python agents.", {})])
    router.post(url__regex=BATCH_URL).mock(return_value=httpx.Response(200, json={"name": "batches/slow"}))
    router.get("https://generativelanguage.googleapis.com/v1beta/batches/slow").mock(return_value=httpx.Response(
        200, json={"name": "batches/slow", "metadata": {"state": "BATCH_STATE_RUNNING"}}))
    ledger, guard = services(connection, bcfg)
    with make_client(bcfg) as client:
        submit_score_batch(connection, bcfg, client, ledger, guard, k=5, clock=clock)
        result = collect_score_batch(connection, bcfg, client, ledger, clock=clock)
    assert result.pending_batches == 1 and result.collected == 0
    assert connection.execute("SELECT status FROM llm_batches").fetchone()["status"] == "pending"


def test_failed_batch_is_marked_and_jobs_can_be_resubmitted(
    connection: sqlite3.Connection, bcfg: Config, clock, router: respx.MockRouter  # noqa: F811
) -> None:
    seed(connection, bcfg, clock, [("Machine Learning Engineer", "Python agents.", {})])
    router.post(url__regex=BATCH_URL).mock(side_effect=[httpx.Response(200, json={"name": "batches/bad"}),
                                                        httpx.Response(200, json={"name": "batches/retry"})])
    router.get("https://generativelanguage.googleapis.com/v1beta/batches/bad").mock(return_value=httpx.Response(
        200, json={"name": "batches/bad", "done": True, "metadata": {"state": "BATCH_STATE_FAILED"}}))
    ledger, guard = services(connection, bcfg)
    with make_client(bcfg) as client:
        submit_score_batch(connection, bcfg, client, ledger, guard, k=5, clock=clock)
        assert collect_score_batch(connection, bcfg, client, ledger, clock=clock).failed == 1
        again = submit_score_batch(connection, bcfg, client, ledger, guard, k=5, clock=clock)
    assert again.submitted == 1


def test_batch_submission_respects_the_budget(connection: sqlite3.Connection, bcfg: Config, clock,  # noqa: F811
                                              router: respx.MockRouter) -> None:
    broke = dataclasses.replace(bcfg, budget=dataclasses.replace(bcfg.budget, monthly_usd=0.0))
    seed(connection, broke, clock, [("Machine Learning Engineer", "Python agents.", {})])
    route = router.post(url__regex=BATCH_URL).mock(return_value=httpx.Response(200, json={"name": "batches/x"}))
    ledger, guard = services(connection, broke)
    with make_client(broke) as client:
        result = submit_score_batch(connection, broke, client, ledger, guard, k=5, clock=clock)
    assert result.submitted == 0 and "budget" in result.message and not route.called
