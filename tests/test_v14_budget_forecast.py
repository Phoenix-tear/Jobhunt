"""Tests for Budget Forecast calculation and Health API warning (Brief v14 Section 1)."""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobhunt.api.app import create_app
from jobhunt.config import Config, load_config
from jobhunt.matching.budget import BudgetForecast, compute_budget_forecast


def _setup_test_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            day TEXT NOT NULL,
            model TEXT NOT NULL,
            purpose TEXT NOT NULL,
            calls INTEGER NOT NULL,
            items INTEGER NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            estimated_usd REAL NOT NULL,
            note TEXT
        )
    """)
    conn.commit()


def test_budget_forecast_empty_db() -> None:
    conn = sqlite3.connect(":memory:")
    _setup_test_db(conn)
    cfg = load_config()
    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

    forecast = compute_budget_forecast(conn, cfg, now)
    assert forecast.month_to_date_usd == 0.0
    assert forecast.avg_daily_usd_7d == 0.0
    assert forecast.projected_month_usd == 0.0
    assert forecast.warning is False
    assert forecast.cap_hit_date is None
    assert forecast.cap_usd == cfg.budget.monthly_usd


def test_budget_forecast_within_cap() -> None:
    conn = sqlite3.connect(":memory:")
    _setup_test_db(conn)
    cfg = load_config()
    cfg = dataclasses.replace(cfg, budget=dataclasses.replace(cfg.budget, monthly_usd=5.0))
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

    # Insert $0.10/day for the last 7 days (Oct 9 to Oct 15)
    for day_num in range(9, 16):
        day_str = f"2026-10-{day_num:02d}"
        conn.execute(
            "INSERT INTO llm_usage (created_at, day, model, purpose, calls, items, input_tokens, output_tokens, estimated_usd) "
            "VALUES (?, ?, 'gemini-2.5-flash', 'score', 10, 10, 1000, 100, 0.10)",
            (f"{day_str}T10:00:00Z", day_str),
        )
    conn.commit()

    forecast = compute_budget_forecast(conn, cfg, now)
    # MTD is 7 * 0.10 = $0.70
    assert forecast.month_to_date_usd == 0.70
    # 7d avg is 0.70 / 7 = $0.10
    assert forecast.avg_daily_usd_7d == 0.10
    # Oct has 31 days. Remaining after Oct 15 is 16 days.
    # Projected: 0.70 + 0.10 * 16 = $2.30
    assert forecast.projected_month_usd == 2.30
    assert forecast.warning is False
    assert forecast.cap_hit_date is None


def test_budget_forecast_warning_and_cap_hit() -> None:
    conn = sqlite3.connect(":memory:")
    _setup_test_db(conn)
    cfg = load_config()
    cfg = dataclasses.replace(cfg, budget=dataclasses.replace(cfg.budget, monthly_usd=5.0))
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

    # High daily spend: $0.40/day for 10 days = $4.00 MTD
    for day_num in range(1, 11):
        day_str = f"2026-10-{day_num:02d}"
        conn.execute(
            "INSERT INTO llm_usage (created_at, day, model, purpose, calls, items, input_tokens, output_tokens, estimated_usd) "
            "VALUES (?, ?, 'gemini-2.5-flash', 'score', 40, 40, 4000, 400, 0.40)",
            (f"{day_str}T10:00:00Z", day_str),
        )
    conn.commit()

    forecast = compute_budget_forecast(conn, cfg, now)
    assert forecast.month_to_date_usd == 4.00
    assert forecast.avg_daily_usd_7d == 0.40
    # Remaining: 31 - 10 = 21 days. Projected: 4.00 + 0.40 * 21 = $12.40
    assert forecast.projected_month_usd == 12.40
    assert forecast.warning is True
    # Cap is $5.00. Remaining budget: $1.00.
    # At $0.40/day: 1.00 / 0.40 = 2.5 days -> 3 days after day 10 = day 13!
    assert forecast.cap_hit_date == "2026-10-13"


def test_budget_forecast_already_exceeded() -> None:
    conn = sqlite3.connect(":memory:")
    _setup_test_db(conn)
    cfg = load_config()
    cfg = dataclasses.replace(cfg, budget=dataclasses.replace(cfg.budget, monthly_usd=5.0))
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

    conn.execute(
        "INSERT INTO llm_usage (created_at, day, model, purpose, calls, items, input_tokens, output_tokens, estimated_usd) "
        "VALUES ('2026-10-15T10:00:00Z', '2026-10-15', 'gemini-2.5-flash', 'score', 50, 50, 5000, 500, 6.00)"
    )
    conn.commit()

    forecast = compute_budget_forecast(conn, cfg, now)
    assert forecast.month_to_date_usd == 6.00
    assert forecast.warning is True
    assert forecast.cap_hit_date == "2026-10-15"


def test_budget_forecast_completed_window_when_today_empty() -> None:
    """Verify that when today has 0 spend (e.g. morning check), the completed 7 days [today-7, today-1]
    are used so the daily average is not artificially diluted by dividing 6 days by 7."""
    conn = sqlite3.connect(":memory:")
    _setup_test_db(conn)
    cfg = load_config()
    now = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)

    # Insert exactly $0.1143 per day for the 7 completed days: Oct 8 to Oct 14
    # Oct 15 (today) has zero spend.
    for day_num in range(8, 15):
        day_str = f"2026-10-{day_num:02d}"
        conn.execute(
            "INSERT INTO llm_usage (created_at, day, model, purpose, calls, items, input_tokens, output_tokens, estimated_usd) "
            "VALUES (?, ?, 'gemini-2.5-flash', 'score', 50, 50, 5000, 500, 0.1143)",
            (f"{day_str}T10:00:00Z", day_str),
        )
    conn.commit()

    forecast = compute_budget_forecast(conn, cfg, now)
    # The 7-day average must reflect the full 7 completed days, exactly 0.1143 / day
    assert forecast.avg_daily_usd_7d == pytest.approx(0.1143, abs=1e-4)


def test_health_api_budget_forecast_endpoint() -> None:
    cfg = load_config()
    app = create_app(cfg, extra_allowed_hosts=("testserver",))
    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "matching" in data
        if data["matching"] is not None:
            assert "budget_forecast" in data["matching"]
            bf = data["matching"]["budget_forecast"]
            assert "month_to_date_usd" in bf
            assert "avg_daily_usd_7d" in bf
            assert "projected_month_usd" in bf
            assert "cap_usd" in bf
            assert "warning" in bf
        assert "budget_forecast" in data

