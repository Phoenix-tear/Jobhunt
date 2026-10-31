"""Spend tracking and hard caps for every paid Gemini call.

Every call writes one `llm_usage` row (model, purpose, tokens, estimated USD; never prompt text).
`BudgetGuard` reads the ledger and says whether the next call is allowed: a daily call cap for
generation, a daily text cap for embeddings, and a monthly USD cap across everything. When a cap
is hit the guard returns a reason, the caller stops, and the reason is logged: nothing
retries around a cap.

Prices come from `config.yaml -> pricing` (copied from Google's pricing page, with the date read).
A model that is not listed there is priced with a deliberately pessimistic fallback.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from jobhunt.config import BudgetSettings, PricingSettings
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)

PURPOSES = ("embed", "classify", "score", "remote")
GENERATE_PURPOSES = ("classify", "score", "remote")
# Calls made on a click (tailor, cover letter, answer) or by benchmarks. They have their own daily limit so they can
# never use up the calls the nightly match needs. "write" is the purpose older rows were recorded with.
INTERACTIVE_PURPOSES = ("tailor", "cover_letter", "answer", "write", "experiment")
_INTERACTIVE_SQL = ",".join(f"'{name}'" for name in INTERACTIVE_PURPOSES)
INTERACTIVE_RESET_NOTE = "resets 05:30 IST"
# LLM triage (the verdict judge) has its own counter: it never uses up the pipeline's or the clicks' calls.
TRIAGE_PURPOSES = ("triage",)
_NON_PIPELINE_SQL = ",".join(f"'{name}'" for name in (*INTERACTIVE_PURPOSES, *TRIAGE_PURPOSES))


def price_per_million(pricing: PricingSettings, model: str, day: date) -> tuple[float, float, bool]:
    """(input USD / 1M tokens, output USD / 1M tokens, listed?) for `model` on `day`."""
    if model.startswith("ollama:"):          # local models cost nothing
        return 0.0, 0.0, True
    entry = pricing.models.get(model)
    if entry is None or (entry.input is None and entry.output is None):
        return pricing.fallback_input_per_million, pricing.fallback_output_per_million, False
    input_price, output_price = entry.input, entry.output
    if entry.changes_on:
        try:
            changes = date.fromisoformat(str(entry.changes_on))
        except ValueError:
            changes = None
        if changes and day >= changes:
            input_price = entry.then_input if entry.then_input is not None else input_price
            output_price = entry.then_output if entry.then_output is not None else output_price
    return (
        input_price if input_price is not None else pricing.fallback_input_per_million,
        output_price if output_price is not None else 0.0,
        True,
    )


def estimate_cost(pricing: PricingSettings, model: str, input_tokens: int, output_tokens: int, day: date) -> float:
    input_price, output_price, _ = price_per_million(pricing, model, day)
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 characters per token) used for dry runs and when the API omits usage."""
    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True)
class UsageSummary:
    day: str
    month: str
    calls_today: int
    generate_calls_today: int        # the pipeline counter (score, classify, remote, ...): `budget.daily_calls`
    interactive_calls_today: int     # tailor, cover_letter, answer, experiment: `budget.interactive_daily_calls`
    embed_texts_today: int
    usd_today: float
    usd_month: float
    tokens_in_month: int
    tokens_out_month: int
    by_purpose_month: dict[str, float]
    triage_calls_today: int = 0      # LLM verdict calls: `budget.triage_daily_calls`


class UsageLedger:
    def __init__(self, connection: sqlite3.Connection, pricing: PricingSettings, *, clock: Clock = utc_now) -> None:
        self._connection = connection
        self._pricing = pricing
        self._clock = clock

    def record(
        self, model: str, purpose: str, *, input_tokens: int, output_tokens: int = 0, price_factor: float = 1.0,
        calls: int = 1, items: int = 1, note: str | None = None,
    ) -> float:
        now = self._clock()
        cost = estimate_cost(self._pricing, model, input_tokens, output_tokens, now.date()) * price_factor
        self._connection.execute(
            """INSERT INTO llm_usage (created_at, day, model, purpose, calls, items, input_tokens, output_tokens,
                                      estimated_usd, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (to_iso(now), now.date().isoformat(), model, purpose, calls, items, input_tokens, output_tokens,
             cost, note),
        )
        return cost

    def summary(self) -> UsageSummary:
        today = self._clock().date()
        day, month = today.isoformat(), today.strftime("%Y-%m")
        row = self._connection.execute(
            """SELECT COALESCE(SUM(calls), 0) AS calls,
                      COALESCE(SUM(CASE WHEN purpose != 'embed' AND purpose NOT IN (%s) THEN calls ELSE 0 END), 0)
                          AS generate_calls,
                      COALESCE(SUM(CASE WHEN purpose IN (%s) THEN calls ELSE 0 END), 0) AS interactive_calls,
                      COALESCE(SUM(CASE WHEN purpose = 'triage' THEN calls ELSE 0 END), 0) AS triage_calls,
                      COALESCE(SUM(CASE WHEN purpose = 'embed' THEN items ELSE 0 END), 0) AS embed_texts,
                      COALESCE(SUM(estimated_usd), 0) AS usd
               FROM llm_usage WHERE day = ?""" % (_NON_PIPELINE_SQL, _INTERACTIVE_SQL), (day,),
        ).fetchone()
        month_row = self._connection.execute(
            """SELECT COALESCE(SUM(estimated_usd), 0) AS usd, COALESCE(SUM(input_tokens), 0) AS tin,
                      COALESCE(SUM(output_tokens), 0) AS tout
               FROM llm_usage WHERE substr(day, 1, 7) = ?""", (month,),
        ).fetchone()
        by_purpose = {
            r["purpose"]: float(r["usd"]) for r in self._connection.execute(
                "SELECT purpose, SUM(estimated_usd) AS usd FROM llm_usage WHERE substr(day, 1, 7) = ? GROUP BY purpose",
                (month,),
            )
        }
        return UsageSummary(
            day=day, month=month, calls_today=int(row["calls"]), generate_calls_today=int(row["generate_calls"]),
            interactive_calls_today=int(row["interactive_calls"]), triage_calls_today=int(row["triage_calls"]),
            embed_texts_today=int(row["embed_texts"]), usd_today=float(row["usd"]), usd_month=float(month_row["usd"]),
            tokens_in_month=int(month_row["tin"]), tokens_out_month=int(month_row["tout"]), by_purpose_month=by_purpose,
        )


class BudgetGuard:
    """Answers "may I make this call?" from the ledger. Caps are inclusive limits."""

    def __init__(self, ledger: UsageLedger, settings: BudgetSettings) -> None:
        self._ledger = ledger
        self._settings = settings

    def check_generate(self, estimated_usd: float = 0.0, *, interactive: bool = False) -> BudgetDecision:
        """`interactive=True` for clicks (tailor, cover letter, answer): their own daily limit; `monthly_usd` is shared."""
        summary = self._ledger.summary()
        if interactive:
            if summary.interactive_calls_today >= self._settings.interactive_daily_calls:
                return self._blocked(
                    f"interactive limit reached today ({summary.interactive_calls_today}/"
                    f"{self._settings.interactive_daily_calls}), {INTERACTIVE_RESET_NOTE}")
        elif summary.generate_calls_today >= self._settings.daily_calls:
            return self._blocked(f"daily call cap reached ({summary.generate_calls_today}/{self._settings.daily_calls})")
        if summary.usd_month + estimated_usd > self._settings.monthly_usd:
            return self._blocked(
                f"monthly spend cap would be exceeded (${summary.usd_month:.2f} + ${estimated_usd:.2f} "
                f"> ${self._settings.monthly_usd:.2f})"
            )
        return BudgetDecision(True)

    def check_triage(self, estimated_usd: float = 0.0, *, enforce_daily: bool = True) -> BudgetDecision:
        """May a verdict call be made? `enforce_daily=False` is for the explicit backfill, which has its own call and dollar limits;
        the monthly cap is shared and always applies."""
        summary = self._ledger.summary()
        if enforce_daily and summary.triage_calls_today >= self._settings.triage_daily_calls:
            return self._blocked(f"triage limit reached today ({summary.triage_calls_today}/{self._settings.triage_daily_calls}), "
                                 f"{INTERACTIVE_RESET_NOTE}")
        if summary.usd_month + estimated_usd > self._settings.monthly_usd:
            return self._blocked(
                f"monthly spend cap would be exceeded (${summary.usd_month:.2f} + ${estimated_usd:.2f} "
                f"> ${self._settings.monthly_usd:.2f})")
        return BudgetDecision(True)

    def check_embed(self, texts: int, estimated_usd: float = 0.0) -> BudgetDecision:
        summary = self._ledger.summary()
        if summary.embed_texts_today + texts > self._settings.daily_embedding_texts:
            return self._blocked(
                f"daily embedding cap would be exceeded ({summary.embed_texts_today}+{texts} > "
                f"{self._settings.daily_embedding_texts})"
            )
        if summary.usd_month + estimated_usd > self._settings.monthly_usd:
            return self._blocked(
                f"monthly spend cap would be exceeded (${summary.usd_month:.2f} + ${estimated_usd:.2f} "
                f"> ${self._settings.monthly_usd:.2f})"
            )
        return BudgetDecision(True)

    @staticmethod
    def _blocked(reason: str) -> BudgetDecision:
        logger.warning("budget cap: %s; no further paid calls this run", reason)
        return BudgetDecision(False, reason)


# --- projection -----------------------------------------------------------------------------------

DEFAULT_SCORE_TOKENS = (1100, 170)       # compact prompt: input / output tokens when nothing was measured yet


@dataclass(frozen=True)
class CostProjection:
    calls_per_day: int
    avg_input_tokens: int
    avg_output_tokens: int
    model: str
    usd_per_job: float
    usd_per_month: float
    usd_per_month_after_price_change: float
    price_change_on: str | None
    based_on_calls: int


def project_monthly(connection: sqlite3.Connection, config: Any, today: date, *, calls_per_day: int | None = None,
                    recent_calls: int = 60) -> CostProjection:
    """Monthly Gemini cost of the nightly scoring at current settings: calls/day x measured average tokens x
    the configured price, today and after the model's announced price change (30-day month)."""
    model = config.models.score
    perday = calls_per_day if calls_per_day is not None else min(config.scoring.top_k, config.budget.daily_calls)
    marker = "%compact%" if config.scoring.prompt_style == "compact" else "%"
    rows = connection.execute(
        """SELECT calls, input_tokens, output_tokens FROM llm_usage
           WHERE purpose IN ('score', 'experiment') AND model = ? AND calls > 0 AND COALESCE(note, '') LIKE ?
           ORDER BY id DESC LIMIT ?""",
        (model, marker, recent_calls)).fetchall()
    calls = sum(int(r[0]) for r in rows)
    if calls >= 5:
        avg_in = round(sum(int(r[1]) for r in rows) / calls)
        avg_out = round(sum(int(r[2]) for r in rows) / calls)
    else:
        avg_in, avg_out = DEFAULT_SCORE_TOKENS
    factor = 0.5 if getattr(config.scoring, "batch", False) else 1.0       # Batch API price
    per_job_now = estimate_cost(config.pricing, model, avg_in, avg_out, today) * factor
    entry = config.pricing.models.get(model)
    change = str(entry.changes_on) if entry is not None and entry.changes_on else None
    per_job_later = per_job_now
    if change:
        try:
            per_job_later = estimate_cost(config.pricing, model, avg_in, avg_out, date.fromisoformat(change)) * factor
        except ValueError:
            pass
    return CostProjection(
        calls_per_day=perday, avg_input_tokens=avg_in, avg_output_tokens=avg_out, model=model,
        usd_per_job=round(per_job_now, 5), usd_per_month=round(per_job_now * perday * 30, 2),
        usd_per_month_after_price_change=round(per_job_later * perday * 30, 2), price_change_on=change,
        based_on_calls=calls,
    )


# --- forecast -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetForecast:
    month_to_date_usd: float
    avg_daily_usd_7d: float
    projected_month_usd: float
    cap_hit_date: str | None
    warning: bool
    cap_usd: float


def compute_budget_forecast(
    connection: sqlite3.Connection,
    config: Any,
    now: datetime,
) -> BudgetForecast:
    """Month-to-date spend, 7-day average daily rate, projected total for the month, cap hit date, and warning."""
    import calendar
    import math
    from datetime import timedelta

    today = now.date()
    month_str = today.strftime("%Y-%m")
    try:
        row = connection.execute(
            "SELECT COALESCE(SUM(estimated_usd), 0.0) FROM llm_usage WHERE substr(day, 1, 7) = ? AND day <= ?",
            (month_str, today.isoformat()),
        ).fetchone()
        mtd_usd = round(float(row[0]), 4)

        # Sum over 7-day window: check both the 7 completed days ending yesterday
        # and the 7 days ending today. Using max ensures an incomplete day (e.g. morning check)
        # does not artificially dilute the daily run rate.
        yesterday = today - timedelta(days=1)
        start_completed = (yesterday - timedelta(days=6)).isoformat()
        end_completed = yesterday.isoformat()
        row_completed = connection.execute(
            "SELECT COALESCE(SUM(estimated_usd), 0.0) FROM llm_usage WHERE day >= ? AND day <= ?",
            (start_completed, end_completed),
        ).fetchone()
        spend_completed = float(row_completed[0]) if row_completed else 0.0

        start_incl = (today - timedelta(days=6)).isoformat()
        end_incl = today.isoformat()
        row_incl = connection.execute(
            "SELECT COALESCE(SUM(estimated_usd), 0.0) FROM llm_usage WHERE day >= ? AND day <= ?",
            (start_incl, end_incl),
        ).fetchone()
        spend_incl = float(row_incl[0]) if row_incl else 0.0

        spend_7d = max(spend_completed, spend_incl)
        avg_7d = round(spend_7d / 7.0, 4)
    except sqlite3.OperationalError:
        mtd_usd = 0.0
        avg_7d = 0.0

    days_in_month = calendar.monthrange(today.year, today.month)[1]
    remaining_days = max(0, days_in_month - today.day)
    projected_total = round(mtd_usd + avg_7d * remaining_days, 2)

    cap = getattr(config.budget, "monthly_usd", 5.0)
    warning = bool(projected_total > cap)
    cap_hit_date = None
    if mtd_usd >= cap:
        cap_hit_date = today.isoformat()
    elif avg_7d > 0:
        remaining_budget = cap - mtd_usd
        days_until = remaining_budget / avg_7d
        hit_day = today.day + math.ceil(days_until)
        if hit_day <= days_in_month:
            cap_hit_date = f"{month_str}-{hit_day:02d}"

    return BudgetForecast(
        month_to_date_usd=mtd_usd,
        avg_daily_usd_7d=avg_7d,
        projected_month_usd=projected_total,
        cap_hit_date=cap_hit_date,
        warning=warning,
        cap_usd=cap,
    )

