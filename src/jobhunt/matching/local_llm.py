"""The local LLM tier (Ollama, 127.0.0.1): workplace extraction, borderline classification, pre-scoring.

Everything here is optional and gated by `local_llm.tasks` in config.yaml, which should only be switched on
for a task whose benchmark (`scripts/bench_local_llm.py`) met its threshold. The stage is sequential,
time-boxed, resumable (a verdict is cached by posting hash + prompt version + model) and ends by unloading
the model so the GPU is free. Job text is untrusted: it sits inside delimiters, only schema-valid JSON is
accepted, and an answer can only ever fill a verdict column. It never changes rules or configuration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from jobhunt.config import Config
from jobhunt.matching.compact import (
    COMPACT_FIT_SCHEMA, COMPACT_SCORE_SYSTEM, CompactFit, compact_description, load_cards,
)
from jobhunt.matching.ollama import OllamaClient, OllamaError, OllamaUnavailable
from jobhunt.matching.prompts import (
    CLASSIFY_SCHEMA, CLASSIFY_SYSTEM, WORKPLACE_SCHEMA, WORKPLACE_SYSTEM, Classification, InvalidAnswer, JobForPrompt,
    WorkplaceAnswer, build_classify_user_text, parse_answer,
)
from jobhunt.matching.scoring import load_profiles
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)

PROMPT_VERSION = "local-v1"


@dataclass
class LocalStats:
    available: bool = True
    model: str = ""
    workplace: int = 0
    classified: int = 0
    prescored: int = 0
    failed: int = 0
    skipped_cached: int = 0
    seconds: float = 0.0
    stopped_reason: str | None = None
    errors: list[str] = field(default_factory=list)


def _digest(description_hash: str | None, model: str, task: str) -> str:
    return hashlib.sha256(f"{task}|{PROMPT_VERSION}|{model}|{description_hash or ''}".encode()).hexdigest()[:20]


def _candidates(connection: sqlite3.Connection, task: str, limit: int) -> list[sqlite3.Row]:
    base = """SELECT j.id, j.title, j.company_name, j.location_raw, j.description_text, j.description_hash,
                     j.workplace_type, r.local_llm_flags, r.local_llm_input_hash, r.local_llm_score,
                     r.gate_status, r.local_rank_score
              FROM jobs j JOIN job_relevance r ON r.job_id = j.id
              WHERE j.status = 'open' AND TRIM(COALESCE(j.description_text, '')) != ''"""
    if task == "workplace":
        sql = base + " AND j.workplace_type = 'unknown' AND r.gate_status IN ('target', 'maybe') " \
                     "ORDER BY COALESCE(r.local_rank_score, r.similarity_ai * 100, 0) DESC LIMIT ?"
    elif task == "classify":
        sql = base + " AND r.gate_status = 'maybe' AND r.llm_status IS NOT 'scored' " \
                     "ORDER BY COALESCE(r.local_rank_score, r.similarity_ai * 100, 0) DESC LIMIT ?"
    else:   # prescore: the ranker's top candidates that Gemini has not scored
        sql = base + " AND r.gate_status IN ('target', 'maybe') AND r.llm_status IS NOT 'scored' " \
                     "ORDER BY COALESCE(r.local_rank_score, r.similarity_ai * 100, 0) DESC LIMIT ?"
    return connection.execute(sql, (limit,)).fetchall()


def run_local_llm(
    connection: sqlite3.Connection, config: Config, client: OllamaClient, *, clock: Clock = utc_now,
    time_box_seconds: float | None = None,
) -> LocalStats:
    settings = config.local_llm
    stats = LocalStats(model=settings.model)
    if not settings.enabled:
        stats.stopped_reason = "local_llm.enabled is false"
        return stats
    if not client.available():
        stats.available = False
        stats.stopped_reason = "Ollama is not running on 127.0.0.1:11434"
        return stats
    deadline = time.monotonic() + (time_box_seconds if time_box_seconds is not None else settings.time_box_seconds)
    started = time.monotonic()
    model = settings.model
    profiles = load_profiles(connection)
    cards = load_cards(profiles, config)
    score_system = COMPACT_SCORE_SYSTEM + "\n\n" + "\n\n".join(
        f'<candidate_card name="{n}">\n{c}\n</candidate_card>' for n, c in sorted(cards.items()))

    def user_text(row: sqlite3.Row) -> str:
        job = JobForPrompt(row["title"], row["company_name"], row["location_raw"], "unknown", "n/a",
                           compact_description(row["description_text"]), description_char_limit=2800)
        return build_classify_user_text(job)[0]

    try:
        for task, enabled, limit in (("workplace", settings.workplace, settings.workplace_limit),
                                     ("classify", settings.classify, settings.classify_limit),
                                     ("prescore", settings.prescore, settings.prescore_top)):
            if not enabled:
                continue
            for row in _candidates(connection, task, limit):
                if time.monotonic() > deadline:
                    stats.stopped_reason = "time box reached (resumable: cached verdicts are kept)"
                    return stats
                digest = _digest(row["description_hash"], model, task)
                flags = _loads(row["local_llm_flags"])
                if flags.get(task, {}).get("digest") == digest:
                    stats.skipped_cached += 1
                    continue
                try:
                    if task == "workplace":
                        answer = _ask(client, model, WORKPLACE_SYSTEM, user_text(row), WORKPLACE_SCHEMA,
                                      WorkplaceAnswer, settings.per_job_timeout_seconds)
                        flags["workplace"] = {"digest": digest, "answer": answer.workplace_type,
                                              "remote_eligible_india": answer.remote_eligible_india}
                        if answer.workplace_type != "unclear":
                            eligible = {"yes": 1, "no": 0}.get(answer.remote_eligible_india) \
                                if answer.workplace_type == "remote" else None
                            connection.execute(
                                """UPDATE jobs SET workplace_type = ?, remote_eligible_india = ?, remote_source = 'local_llm',
                                          remote_evidence = substr(COALESCE(remote_evidence, '') || ' | local LLM: ' || ?, 1, 500)
                                   WHERE id = ? AND workplace_type = 'unknown'""",
                                (answer.workplace_type, eligible, answer.reason, row["id"]))
                        stats.workplace += 1
                    elif task == "classify":
                        answer = _ask(client, model, CLASSIFY_SYSTEM, user_text(row), CLASSIFY_SCHEMA, Classification,
                                      settings.per_job_timeout_seconds)
                        flags["classify"] = {"digest": digest, "relevant_role": answer.relevant_role,
                                             "role_family": answer.role_family}
                        stats.classified += 1
                    else:
                        answer = _ask(client, model, score_system, user_text(row), COMPACT_FIT_SCHEMA, CompactFit,
                                      settings.per_job_timeout_seconds)
                        flags["prescore"] = {"digest": digest, "recommended_profile": answer.recommended_profile,
                                             "is_excluded_role": answer.is_excluded_role}
                        connection.execute("UPDATE job_relevance SET local_llm_score = ? WHERE job_id = ?",
                                           (float(answer.fit_score), row["id"]))
                        stats.prescored += 1
                except (OllamaError, InvalidAnswer, ValidationError) as error:
                    stats.failed += 1
                    if len(stats.errors) < 5:
                        stats.errors.append(f"{task} job {row['id']}: {type(error).__name__}: {str(error)[:80]}")
                    if isinstance(error, OllamaUnavailable):
                        stats.stopped_reason = "Ollama went away"
                        return stats
                    continue
                connection.execute(
                    "UPDATE job_relevance SET local_llm_flags = ?, local_llm_model = ?, local_llm_input_hash = ? "
                    "WHERE job_id = ?", (json.dumps(flags), model, digest, row["id"]))
                connection.commit()
    finally:
        client.unload(model)              # always hand the GPU back
        stats.seconds = round(time.monotonic() - started, 1)
        connection.commit()
    return stats


def _ask(client: OllamaClient, model: str, system: str, user: str, schema: dict[str, Any], model_class: Any,
         timeout: float) -> Any:
    last: Exception | None = None
    for _ in range(2):                           # one retry
        try:
            result = client.chat_json(model, system=system, user=user, schema=schema, max_tokens=300, timeout=timeout)
            return parse_answer(result.text, model_class)
        except OllamaUnavailable:
            raise
        except (OllamaError, InvalidAnswer) as error:
            last = error
    assert last is not None
    raise last


def _loads(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value) if value else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def record_stage(connection: sqlite3.Connection, stage: str, seconds: float, items: int, model: str,
                 status: str = "ok", detail: str | None = None, *, clock: Clock = utc_now) -> None:
    connection.execute(
        "INSERT INTO local_runs (stage, started_at, seconds, items, model, status, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (stage, to_iso(clock()), round(seconds, 1), items, model, status, (detail or "")[:200]))
    connection.commit()
