"""Two daily counters: pipeline (score, classify) and interactive (tailor, cover_letter, answer, experiment)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from jobhunt.config import BudgetSettings, Config
from jobhunt.matching.budget import BudgetGuard, UsageLedger

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def ledger(connection: sqlite3.Connection, config: Config) -> UsageLedger:
    return UsageLedger(connection, config.pricing, clock=lambda: NOW)


def spend(ledger: UsageLedger, purpose: str, count: int, model: str = "gemini-3.8-flash") -> None:
    for _ in range(count):
        ledger.record(model, purpose, input_tokens=100, output_tokens=10)


def guard_for(ledger: UsageLedger, **overrides: float) -> BudgetGuard:
    settings = BudgetSettings(daily_calls=60, interactive_daily_calls=40, monthly_usd=5.0, **overrides)  # type: ignore[arg-type]
    return BudgetGuard(ledger, settings)


def test_defaults_keep_the_existing_numbers_and_add_forty(config: Config) -> None:
    assert config.budget.interactive_daily_calls == 40
    assert BudgetSettings().daily_calls == 200 and BudgetSettings().monthly_usd == 10.0


def test_forty_tailor_calls_do_not_stop_sixty_score_calls(ledger: UsageLedger) -> None:
    guard = guard_for(ledger)
    spend(ledger, "tailor", 40)
    summary = ledger.summary()
    assert summary.interactive_calls_today == 40 and summary.generate_calls_today == 0
    assert not guard.check_generate(0.01, interactive=True)            # the interactive limit is used up
    for _ in range(60):                                                # ...but match still gets all 60
        assert guard.check_generate(0.001), ledger.summary()
        spend(ledger, "score", 1)
    assert ledger.summary().generate_calls_today == 60
    assert "daily call cap reached (60/60)" in guard.check_generate(0.0).reason


def test_pipeline_calls_do_not_use_the_interactive_limit(ledger: UsageLedger) -> None:
    guard = guard_for(ledger)
    spend(ledger, "score", 60)
    spend(ledger, "classify", 5)
    assert guard.check_generate(0.0, interactive=True)                 # 40 interactive calls are still free
    assert ledger.summary().interactive_calls_today == 0


@pytest.mark.parametrize("purpose", ["tailor", "cover_letter", "answer", "experiment", "write"])
def test_interactive_purposes_count_against_the_interactive_limit(ledger: UsageLedger, purpose: str) -> None:
    spend(ledger, purpose, 3)
    summary = ledger.summary()
    assert summary.interactive_calls_today == 3 and summary.generate_calls_today == 0


def test_the_interactive_message_says_when_it_resets(ledger: UsageLedger) -> None:
    spend(ledger, "answer", 40)
    decision = guard_for(ledger).check_generate(0.0, interactive=True)
    assert not decision
    assert decision.reason == "interactive limit reached today (40/40), resets 05:30 IST"


def test_embedding_calls_count_in_neither_counter(ledger: UsageLedger) -> None:
    ledger.record("gemini-embedding-2", "embed", input_tokens=1000, items=500)
    summary = ledger.summary()
    assert summary.generate_calls_today == 0 and summary.interactive_calls_today == 0 and summary.embed_texts_today == 500


def test_the_monthly_cap_is_shared_and_blocks_both(ledger: UsageLedger) -> None:
    broke = BudgetGuard(ledger, BudgetSettings(daily_calls=60, interactive_daily_calls=40, monthly_usd=0.0))
    pipeline, interactive = broke.check_generate(0.01), broke.check_generate(0.01, interactive=True)
    assert not pipeline and "monthly spend cap" in pipeline.reason
    assert not interactive and "monthly spend cap" in interactive.reason


def test_monthly_spend_from_one_side_blocks_the_other(ledger: UsageLedger) -> None:
    guard = BudgetGuard(ledger, BudgetSettings(daily_calls=60, interactive_daily_calls=40, monthly_usd=0.001))
    ledger.record("gemini-2.5-pro", "tailor", input_tokens=200_000, output_tokens=50_000)
    assert ledger.summary().usd_month > 0.001
    assert not guard.check_generate(0.0001)                            # a score call is blocked by tailoring spend
    assert "monthly spend cap" in guard.check_generate(0.0001).reason
