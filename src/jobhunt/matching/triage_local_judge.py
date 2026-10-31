"""Tier 2: Local LLM judge using Ollama instruct model (Brief v9).

Uses an installed local instruct model (qwen2.5:7b-instruct, llama 3.1 8b, or gemma 2 9b).
Runs sequentially, 1 job per call, with exact verbatim-quote verification,
JSON schema validation, per-call timeout, and per-run time cap.
Includes an accuracy gate before it can act on job statuses.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunt.config import Config
from jobhunt.matching.ollama import OllamaClient, OllamaError, OllamaUnavailable
from jobhunt.matching.triage import (
    TRIAGE_SYSTEM,
    VERDICT_SCHEMA,
    Evidence,
    Verdict,
    build_evidence,
    verify,
)
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)

# Preferred models in priority order per brief
PREFERRED_MODELS = (
    "qwen2.5:7b-instruct",
    "qwen2.5:7b",
    "llama3.1:8b-instruct",
    "llama3.1:8b",
    "llama3.1:latest",
    "gemma2:9b-instruct",
    "gemma2:9b",
)

GATE_FILE = "data/models/triage_local_llm_gate.json"


def select_best_instruct_model(client: OllamaClient, preferred_configured: str | None = None) -> str | None:
    """Select the best available instruct model from Ollama, matching brief priority."""
    if not client.available():
        return None
    try:
        available = client.models()
    except Exception as err:
        logger.warning("Failed to list Ollama models: %s", err)
        return None

    if not available:
        return None

    avail_lower = {m.lower(): m for m in available}

    # If configured model is explicitly available, use it
    if preferred_configured and preferred_configured.lower() in avail_lower:
        return avail_lower[preferred_configured.lower()]

    # Check preferred candidates
    for cand in PREFERRED_MODELS:
        cand_lower = cand.lower()
        if cand_lower in avail_lower:
            return avail_lower[cand_lower]
        # Partial match (e.g. qwen2.5:7b-instruct-q4_K_M)
        for m_low, original in avail_lower.items():
            if cand_lower in m_low or (cand_lower.split(":")[0] in m_low and "instruct" in m_low):
                return original

    # Any model with 'instruct' in name
    for m_low, original in avail_lower.items():
        if "instruct" in m_low:
            return original

    return None


@dataclass
class LocalJudgeVerdictResult:
    job_id: int
    verdict: Verdict
    raw: dict[str, Any]
    from_cache: bool = False
    seconds: float = 0.0
    error: str | None = None


@dataclass
class LocalJudgeRunSummary:
    model: str
    total_requested: int = 0
    from_cache: int = 0
    judged: int = 0
    failed: int = 0
    hallucinated_fields: int = 0
    claimed_fields: int = 0
    total_seconds: float = 0.0
    stopped_reason: str | None = None
    results: list[LocalJudgeVerdictResult] = field(default_factory=list)


@dataclass
class AccuracyGateResult:
    model: str
    n_evaluated: int
    field_metrics: dict[str, dict[str, Any]]
    hallucinated_quote_rate: float
    avg_seconds_per_job: float
    can_act: dict[str, bool]     # field -> bool: whether local LLM precision >= 90% with >= 30 examples
    overall_can_act: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "n_evaluated": self.n_evaluated,
            "field_metrics": self.field_metrics,
            "hallucinated_quote_rate": round(self.hallucinated_quote_rate, 4),
            "avg_seconds_per_job": round(self.avg_seconds_per_job, 2),
            "can_act": self.can_act,
            "overall_can_act": self.overall_can_act,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AccuracyGateResult":
        return cls(
            model=d["model"],
            n_evaluated=int(d["n_evaluated"]),
            field_metrics=d["field_metrics"],
            hallucinated_quote_rate=float(d.get("hallucinated_quote_rate", 0.0)),
            avg_seconds_per_job=float(d.get("avg_seconds_per_job", 0.0)),
            can_act=d.get("can_act", {}),
            overall_can_act=bool(d.get("overall_can_act", False)),
        )


class TriageLocalJudge:
    """Local LLM judge (Tier 2) executing instruct models via Ollama."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        config: Config,
        *,
        client: OllamaClient | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.connection = connection
        self.config = config
        self.client = client or OllamaClient()
        self.clock = clock
        self.rubric_version = config.triage.rubric_version
        self.model = select_best_instruct_model(self.client, config.triage.local_llm_model)

    def is_available(self) -> bool:
        if not getattr(self.config.triage, "enabled", True):
            return False
        return self.model is not None and self.client.available()

    def unload_model(self) -> None:
        """Release the GPU memory so embedding and other models do not fight for VRAM."""
        if self.model and self.client:
            self.client.unload(self.model)

    def get_cached_verdict(self, job_id: int, content_hash: str) -> Verdict | None:
        """Check if a verified verdict exists for this content hash, rubric, and model."""
        if not self.model:
            return None
        row = self.connection.execute(
            """
            SELECT verified_json FROM job_llm_verdicts
            WHERE job_id = ? AND content_hash = ? AND rubric_version = ? AND model = ?
            """,
            (job_id, content_hash, self.rubric_version, self.model),
        ).fetchone()
        if row and row["verified_json"]:
            try:
                data = json.loads(row["verified_json"])
                return Verdict.from_json(data)
            except Exception:
                return None
        return None

    def store_verdict(
        self,
        job_id: int,
        content_hash: str,
        raw: dict[str, Any],
        verdict: Verdict,
        now_iso: str,
    ) -> None:
        """Store verdict in job_llm_verdicts with source='local_llm'."""
        self.connection.execute(
            """
            INSERT OR REPLACE INTO job_llm_verdicts (
                job_id, content_hash, rubric_version, model, raw_json, verified_json,
                hallucinated, cost_usd, created_at, source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, ?, 'local_llm')
            """,
            (
                job_id,
                content_hash,
                self.rubric_version,
                self.model or "unknown",
                json.dumps(raw, ensure_ascii=False),
                json.dumps(verdict.as_json(), ensure_ascii=False),
                len(verdict.hallucinated),
                now_iso,
            ),
        )

    def judge_one(
        self,
        job_id: int,
        evidence: Evidence,
        *,
        timeout: float = 90.0,
        now_iso: str | None = None,
    ) -> LocalJudgeVerdictResult:
        """Judge a single job with the local LLM, verifying verbatim quotes."""
        if not self.model or not self.client.available():
            return LocalJudgeVerdictResult(
                job_id=job_id,
                verdict=Verdict(),
                raw={},
                error="Local LLM model not available on Ollama",
            )

        content_hash = evidence.content_hash
        cached = self.get_cached_verdict(job_id, content_hash)
        if cached is not None:
            return LocalJudgeVerdictResult(job_id=job_id, verdict=cached, raw={}, from_cache=True)

        user_text = (
            f'<job_posting number="1">\n'
            f"title: {evidence.title}\n"
            f"company: {evidence.company}\n"
            f"location: {evidence.location}\n"
            f"workplace: {evidence.workplace}\n"
            f"extract:\n{evidence.extract}\n"
            f"</job_posting>"
        )

        try:
            chat_res = self.client.chat_json(
                model=self.model,
                system=TRIAGE_SYSTEM,
                user=user_text,
                schema=VERDICT_SCHEMA,
                max_tokens=600,
                num_ctx=3072,
                timeout=timeout,
            )
        except Exception as err:
            logger.warning("Local LLM call failed for job %d: %s", job_id, err)
            return LocalJudgeVerdictResult(
                job_id=job_id,
                verdict=Verdict(),
                raw={},
                error=f"{type(err).__name__}: {str(err)[:150]}",
            )

        # Parse output JSON
        raw_dict: dict[str, Any] = {}
        try:
            parsed = json.loads(chat_res.text)
            if isinstance(parsed, dict):
                if "verdicts" in parsed and isinstance(parsed["verdicts"], list) and parsed["verdicts"]:
                    raw_dict = parsed["verdicts"][0]
                elif "level" in parsed or "india_eligible" in parsed:
                    raw_dict = parsed
        except Exception as err:
            logger.warning("Failed to parse local LLM JSON for job %d: %s", job_id, err)
            return LocalJudgeVerdictResult(
                job_id=job_id,
                verdict=Verdict(),
                raw={},
                seconds=chat_res.seconds,
                error="Invalid JSON response",
            )

        # Verify against verbatim shown extract
        verdict = verify(raw_dict, evidence.shown, title=evidence.title)

        # Cache verdict
        ts = now_iso or to_iso(self.clock())
        self.store_verdict(job_id, content_hash, raw_dict, verdict, ts)
        self.connection.commit()

        return LocalJudgeVerdictResult(
            job_id=job_id,
            verdict=verdict,
            raw=raw_dict,
            seconds=chat_res.seconds,
        )

    def judge_jobs(
        self,
        job_ids: list[int],
        evidences: dict[int, Evidence],
        *,
        max_minutes: float = 60.0,
        timeout_per_job: float = 90.0,
    ) -> LocalJudgeRunSummary:
        """Run local LLM sequentially on jobs with per-run time cap and auto GPU unload."""
        summary = LocalJudgeRunSummary(model=self.model or "none", total_requested=len(job_ids))
        if not self.is_available():
            summary.stopped_reason = "Ollama instruct model not available"
            return summary

        start_time = time.monotonic()
        max_seconds = max_minutes * 60.0
        now_iso = to_iso(self.clock())

        try:
            for jid in job_ids:
                if (time.monotonic() - start_time) > max_seconds:
                    summary.stopped_reason = f"per-run cap of {max_minutes} minutes reached"
                    break

                ev = evidences.get(jid)
                if not ev:
                    continue

                res = self.judge_one(jid, ev, timeout=timeout_per_job, now_iso=now_iso)
                summary.results.append(res)
                summary.total_seconds += res.seconds

                if res.from_cache:
                    summary.from_cache += 1
                elif res.error:
                    summary.failed += 1
                else:
                    summary.judged += 1
                    summary.hallucinated_fields += len(res.verdict.hallucinated)
                    # Count claimed fields
                    claims = sum(
                        1
                        for k in ("level", "required_years_min", "india_eligible", "eligibility_blockers")
                        if res.raw.get(k) not in (None, "", "unknown", "unclear", -1, [])
                    )
                    summary.claimed_fields += claims

        finally:
            # Release GPU VRAM for embeddings and other processes
            if self.model:
                self.client.unload(self.model)

        return summary


def run_accuracy_gate(
    connection: sqlite3.Connection,
    config: Config,
    *,
    client: OllamaClient | None = None,
    limit: int = 50,
    min_high_conf_samples: int = 30,
    min_precision: float = 0.90,
) -> AccuracyGateResult:
    """Benchmark local LLM against jobs with verified Gemini verdicts and human labels.

    Evaluates:
    - level: senior vs non-senior
    - years: required_years >= 2 vs < 2
    - india_eligible: yes vs no
    - blockers: detected blocker agreement
    """
    judge = TriageLocalJudge(connection, config, client=client)
    if not judge.is_available():
        return AccuracyGateResult(
            model="none",
            n_evaluated=0,
            field_metrics={},
            hallucinated_quote_rate=0.0,
            avg_seconds_per_job=0.0,
            can_act={},
            overall_can_act=False,
        )

    # Fetch ground truth: jobs that have verified Gemini verdicts
    rows = connection.execute(
        """
        SELECT v.job_id, v.content_hash, v.verified_json, j.title, j.company_name, j.location_raw,
               j.workplace_type, j.remote_regions, j.description_text
        FROM job_llm_verdicts v
        JOIN jobs j ON j.id = v.job_id
        WHERE v.source = 'gemini' AND v.hallucinated = 0
        ORDER BY v.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        return AccuracyGateResult(
            model=judge.model or "none",
            n_evaluated=0,
            field_metrics={},
            hallucinated_quote_rate=0.0,
            avg_seconds_per_job=0.0,
            can_act={},
            overall_can_act=False,
        )

    results: list[tuple[Verdict, Verdict, float]] = []  # (gemini_verdict, local_verdict, seconds)
    total_quotes = 0
    hallucinated_quotes = 0

    for r in rows:
        jid = int(r["job_id"])
        gemini_data = json.loads(r["verified_json"])
        gemini_v = Verdict.from_json(gemini_data)

        ev = build_evidence(
            r["title"] or "",
            r["company_name"] or "",
            r["location_raw"],
            r["workplace_type"],
            r["remote_regions"],
            r["description_text"],
        )

        res = judge.judge_one(jid, ev)
        if not res.error:
            results.append((gemini_v, res.verdict, res.seconds))
            total_quotes += (
                bool(res.verdict.level_quote)
                + bool(res.verdict.years_quote)
                + bool(res.verdict.india_quote)
                + bool(res.verdict.blocker_quote)
                + len(res.verdict.hallucinated)
            )
            hallucinated_quotes += len(res.verdict.hallucinated)

    # Release GPU
    if judge.model:
        judge.client.unload(judge.model)

    n_eval = len(results)
    if n_eval == 0:
        return AccuracyGateResult(
            model=judge.model or "none",
            n_evaluated=0,
            field_metrics={},
            hallucinated_quote_rate=0.0,
            avg_seconds_per_job=0.0,
            can_act={},
            overall_can_act=False,
        )

    avg_secs = sum(s for _, _, s in results) / max(n_eval, 1)
    halluc_rate = hallucinated_quotes / max(total_quotes, 1)

    field_metrics: dict[str, dict[str, Any]] = {}
    can_act: dict[str, bool] = {}

    # 1. Seniority head: level in (senior, lead_or_manager)
    sen_high_conf = [
        (g.level in ("senior", "lead_or_manager"), l.level in ("senior", "lead_or_manager"))
        for g, l, _ in results
        if l.confidence == "high" and l.level != "unknown"
    ]
    if sen_high_conf:
        correct = sum(1 for gt, pred in sen_high_conf if gt == pred)
        prec = correct / len(sen_high_conf)
        pos_preds = [gt for gt, pred in sen_high_conf if pred]
        pos_prec = sum(pos_preds) / max(len(pos_preds), 1)
        field_metrics["seniority"] = {
            "n_samples": len(sen_high_conf),
            "accuracy": round(prec, 4),
            "precision_on_positive": round(pos_prec, 4),
        }
        can_act["seniority"] = len(sen_high_conf) >= min_high_conf_samples and pos_prec >= min_precision
    else:
        field_metrics["seniority"] = {"n_samples": 0, "accuracy": 0.0, "precision_on_positive": 0.0}
        can_act["seniority"] = False

    # 2. Location head: not India eligible
    loc_high_conf = [
        (g.india_eligible == "no", l.india_eligible == "no")
        for g, l, _ in results
        if l.confidence == "high" and l.india_eligible in ("yes", "no")
    ]
    if loc_high_conf:
        correct = sum(1 for gt, pred in loc_high_conf if gt == pred)
        prec = correct / len(loc_high_conf)
        pos_preds = [gt for gt, pred in loc_high_conf if pred]
        pos_prec = sum(pos_preds) / max(len(pos_preds), 1)
        field_metrics["location"] = {
            "n_samples": len(loc_high_conf),
            "accuracy": round(prec, 4),
            "precision_on_positive": round(pos_prec, 4),
        }
        can_act["location"] = len(loc_high_conf) >= min_high_conf_samples and pos_prec >= min_precision
    else:
        field_metrics["location"] = {"n_samples": 0, "accuracy": 0.0, "precision_on_positive": 0.0}
        can_act["location"] = False

    # 3. Years head: required_years_min >= 2
    yr_high_conf = [
        ((g.required_years_min or 0) >= 2, (l.required_years_min or 0) >= 2)
        for g, l, _ in results
        if l.confidence == "high" and l.required_years_min is not None
    ]
    if yr_high_conf:
        correct = sum(1 for gt, pred in yr_high_conf if gt == pred)
        prec = correct / len(yr_high_conf)
        pos_preds = [gt for gt, pred in yr_high_conf if pred]
        pos_prec = sum(pos_preds) / max(len(pos_preds), 1)
        field_metrics["years"] = {
            "n_samples": len(yr_high_conf),
            "accuracy": round(prec, 4),
            "precision_on_positive": round(pos_prec, 4),
        }
        can_act["years"] = len(yr_high_conf) >= min_high_conf_samples and pos_prec >= min_precision
    else:
        field_metrics["years"] = {"n_samples": 0, "accuracy": 0.0, "precision_on_positive": 0.0}
        can_act["years"] = False

    overall_can_act = any(can_act.values())

    gate_result = AccuracyGateResult(
        model=judge.model or "unknown",
        n_evaluated=n_eval,
        field_metrics=field_metrics,
        hallucinated_quote_rate=halluc_rate,
        avg_seconds_per_job=avg_secs,
        can_act=can_act,
        overall_can_act=overall_can_act,
    )

    # Save to model cache
    p = config.resolve_path(GATE_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(gate_result.to_dict(), indent=2), encoding="utf-8")

    return gate_result


def load_accuracy_gate(config: Config) -> AccuracyGateResult | None:
    p = config.resolve_path(GATE_FILE)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return AccuracyGateResult.from_dict(data)
    except Exception as err:
        logger.warning("Failed to load local LLM accuracy gate: %s", err)
        return None


__all__ = [
    "AccuracyGateResult",
    "LocalJudgeRunSummary",
    "LocalJudgeVerdictResult",
    "PREFERRED_MODELS",
    "TriageLocalJudge",
    "load_accuracy_gate",
    "run_accuracy_gate",
    "select_best_instruct_model",
]
