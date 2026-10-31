"""Benchmark local Ollama chat models against Gemini's stored verdicts.

python scripts/bench_local_llm.py qwen3:4b qwen3:8b llama3.2:3b [--limit 60]
Tasks: (a) workplace extraction (truth: structured source fields), (b) relevant-role classification (truth: stored
Gemini triage), (c) pre-scoring (truth: stored Gemini fit scores; Spearman and top-20 overlap).
Gate thresholds (also stated in the README):  extraction accuracy >= 0.85,  classification accuracy >= 0.80,
pre-scoring Spearman >= 0.50 and top-20 overlap >= 8 of 20.
Reads the database read-only; writes data/eval/local_llm_benchmark.json.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
from jobhunt.matching.stats import spearman_or_nan

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jobhunt.config import load_config  # noqa: E402
from jobhunt.matching.compact import COMPACT_FIT_SCHEMA, COMPACT_SCORE_SYSTEM, CompactFit, load_cards  # noqa: E402
from jobhunt.matching.ollama import OllamaClient, OllamaError  # noqa: E402
from jobhunt.matching.prompts import (  # noqa: E402
    CLASSIFY_SCHEMA, CLASSIFY_SYSTEM, WORKPLACE_SCHEMA, WORKPLACE_SYSTEM, Classification, InvalidAnswer, JobForPrompt,
    WorkplaceAnswer, build_classify_user_text, parse_answer,
)
from jobhunt.matching.scoring import load_profiles  # noqa: E402

THRESHOLDS = {"extraction_accuracy": 0.85, "classification_accuracy": 0.80, "prescore_spearman": 0.50,
              "prescore_top20_overlap": 8}


def vram_mb() -> int:
    try:
        return int(subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                  capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return 0


def job_block(row: sqlite3.Row, *, with_workplace: bool = False, limit: int = 2800) -> tuple[str, bool]:
    from jobhunt.matching.compact import compact_description

    job = JobForPrompt(row["title"], row["company_name"], row["location_raw"],
                       row["workplace_type"] if with_workplace else "unknown", "n/a",
                       compact_description(row["description_text"]), description_char_limit=limit)
    return build_classify_user_text(job)


def ask(client: OllamaClient, model: str, system: str, user: str, schema: dict, model_class, timeout: float = 90.0):
    last = None
    for _ in range(2):
        try:
            result = client.chat_json(model, system=system, user=user, schema=schema, max_tokens=300, timeout=timeout)
            return parse_answer(result.text, model_class), result.seconds
        except (OllamaError, InvalidAnswer) as error:
            last = error
    raise last  # type: ignore[misc]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("models", nargs="+")
    parser.add_argument("--limit", type=int, default=60)
    args = parser.parse_args()

    config = load_config(ROOT / "config.yaml")
    db = sqlite3.connect(f"file:{ROOT / 'data/jobhunt.sqlite3'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    cols = ("j.id, j.title, j.company_name, j.location_raw, j.description_text, j.workplace_type, r.llm_score, "
            "r.llm_flags, j.remote_source")
    has_text = "TRIM(COALESCE(j.description_text, '')) != ''"
    scored = db.execute(f"SELECT {cols} FROM jobs j JOIN job_relevance r ON r.job_id = j.id "
                        f"WHERE r.llm_status = 'scored' AND r.llm_score IS NOT NULL AND {has_text} "
                        "ORDER BY r.llm_score DESC").fetchall()
    classified = db.execute(
        f"SELECT {cols} FROM jobs j JOIN job_relevance r ON r.job_id = j.id WHERE {has_text} "
        "AND json_extract(r.llm_flags, '$.classification.relevant_role') IS NOT NULL").fetchall()
    structured = db.execute(
        f"SELECT {cols} FROM jobs j JOIN job_relevance r ON r.job_id = j.id WHERE {has_text} AND j.status = 'open' "
        "AND j.remote_source = 'structured' AND j.workplace_type IN ('remote', 'hybrid', 'onsite') "
        "ORDER BY j.id LIMIT 400").fetchall()
    by_type: dict[str, list] = {}
    for row in structured:
        by_type.setdefault(row["workplace_type"], []).append(row)
    extraction_set = [r for rows in by_type.values() for r in rows[: args.limit // 3 + 1]]
    step = max(1, len(scored) // args.limit)
    prescore_set = scored[::step][: args.limit]
    profiles = load_profiles(db)
    cards = load_cards(profiles, config)
    system_score = COMPACT_SCORE_SYSTEM + "\n\n" + "\n\n".join(
        f'<candidate_card name="{n}">\n{c}\n</candidate_card>' for n, c in sorted(cards.items()))
    print(f"extraction n={len(extraction_set)} classification n={len(classified)} prescore n={len(prescore_set)}")

    results: dict[str, dict] = {"thresholds": THRESHOLDS}
    with OllamaClient(timeout=180) as client:
        if not client.available():
            sys.exit("Ollama is not running on 127.0.0.1:11434")
        for model in args.models:
            out: dict = {}
            peak = vram_mb()
            # (a) workplace extraction
            correct = n = failed = abstained = 0
            seconds = []
            for row in extraction_set:
                user, _ = job_block(row)
                try:
                    answer, took = ask(client, model, WORKPLACE_SYSTEM, user, WORKPLACE_SCHEMA, WorkplaceAnswer)
                except (OllamaError, InvalidAnswer):
                    failed += 1
                    continue
                seconds.append(took)
                if answer.workplace_type == "unclear":      # the posting may simply not say: that is not an error
                    abstained += 1
                    continue
                n += 1
                correct += int(answer.workplace_type == row["workplace_type"])
            asked = n + abstained
            out["extraction"] = {"asked": asked, "answered": n, "abstained": abstained, "failed": failed,
                                 "accuracy_when_answered": round(correct / n, 3) if n else None,
                                 "answer_rate": round(n / asked, 3) if asked else None,
                                 "sec_per_job": round(float(np.mean(seconds)), 2) if seconds else None}
            peak = max(peak, vram_mb())
            # (b) classification
            correct = n = failed = 0
            seconds = []
            for row in classified:
                truth = json.loads(row["llm_flags"])["classification"]["relevant_role"]
                user, _ = job_block(row)
                try:
                    answer, took = ask(client, model, CLASSIFY_SYSTEM, user, CLASSIFY_SCHEMA, Classification)
                except (OllamaError, InvalidAnswer):
                    failed += 1
                    continue
                seconds.append(took)
                n += 1
                correct += int(bool(answer.relevant_role) == bool(truth))
            out["classification"] = {"n": n, "failed": failed, "accuracy": round(correct / n, 3) if n else None,
                                     "sec_per_job": round(float(np.mean(seconds)), 2) if seconds else None}
            peak = max(peak, vram_mb())
            # (c) pre-scoring
            local, stored, seconds, failed = [], [], [], 0
            for row in prescore_set:
                user, _ = job_block(row)
                try:
                    answer, took = ask(client, model, system_score, user, COMPACT_FIT_SCHEMA, CompactFit, timeout=120)
                except (OllamaError, InvalidAnswer):
                    failed += 1
                    continue
                seconds.append(took)
                local.append(float(answer.fit_score))
                stored.append(float(row["llm_score"]))
            if len(local) >= 10:
                a, b = np.array(local), np.array(stored)
                top = 20 if len(a) >= 40 else max(3, len(a) // 3)
                out["prescore"] = {
                    "n": len(a), "failed": failed, "spearman": round(spearman_or_nan(a, b), 3),
                    "mean_abs_diff": round(float(np.mean(np.abs(a - b))), 1),
                    f"top{top}_overlap": len(set(np.argsort(-a)[:top]) & set(np.argsort(-b)[:top])), "top_n": top,
                    "sec_per_job": round(float(np.mean(seconds)), 2),
                }
            peak = max(peak, vram_mb())
            out["vram_peak_mb"] = peak
            results[model] = out
            print(model, json.dumps(out), flush=True)
            client.unload(model)
            time.sleep(2)
    (ROOT / "data/eval/local_llm_benchmark.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("wrote data/eval/local_llm_benchmark.json  date", date.today())


if __name__ == "__main__":
    main()
