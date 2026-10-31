"""Benchmark local (Ollama) embedding models against the stored Gemini embeddings and Gemini fit scores.

Usage: python scripts/bench_embeddings.py nomic-embed-text mxbai-embed-large all-minilm ...
Writes data/eval/embedding_benchmark.json. Reads the database read-only; nothing is stored.
"""

from __future__ import annotations

import json
import random
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from jobhunt.matching.stats import spearman_or_nan

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jobhunt.matching.embeddings import format_job_text  # noqa: E402
from jobhunt.matching.ollama import OllamaClient  # noqa: E402

PREFIXES = {"nomic-embed-text": ("search_document: ", "search_query: ")}


def vram_mb() -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        return int(out)
    except Exception:  # noqa: BLE001
        return None


def unit(matrix: np.ndarray) -> np.ndarray:
    return matrix / np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-9)


def main(models: list[str], extra: int = 400, seed: int = 5) -> None:
    connection = sqlite3.connect(f"file:{ROOT / 'data/jobhunt.sqlite3'}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    profiles = {r["name"]: r["stripped_text"] for r in connection.execute("SELECT name, stripped_text FROM profiles")}
    columns = ("j.id, j.title, j.company_name, j.location_raw, j.description_text, r.similarity_ai, "
               "r.similarity_fullstack, r.llm_score")
    scored = connection.execute(
        f"SELECT {columns} FROM jobs j JOIN job_relevance r ON r.job_id = j.id "
        "WHERE r.llm_status = 'scored' AND r.llm_score IS NOT NULL AND r.similarity_ai IS NOT NULL "
        "AND TRIM(COALESCE(j.description_text, '')) != ''").fetchall()
    others = connection.execute(
        f"SELECT {columns} FROM jobs j JOIN job_relevance r ON r.job_id = j.id "
        "WHERE j.status = 'open' AND r.similarity_ai IS NOT NULL AND r.llm_score IS NULL "
        "AND TRIM(COALESCE(j.description_text, '')) != ''").fetchall()
    random.Random(seed).shuffle(others)
    rows = scored + others[:extra]
    n_scored = len(scored)
    gemini_sim = np.array([max(r["similarity_ai"], r["similarity_fullstack"] or 0) for r in rows])
    fit = np.array([r["llm_score"] for r in rows[:n_scored]], dtype=float)
    baseline = {
        "spearman_gemini_embedding_vs_fit": spearman_or_nan(gemini_sim[:n_scored], fit),
        "top50_overlap_gemini_embedding_vs_fit": len(set(np.argsort(-gemini_sim[:n_scored])[:50])
                                                     & set(np.argsort(-fit)[:50])),
    }
    print(f"jobs: {n_scored} Gemini-scored + {len(rows) - n_scored} other; baseline {baseline}")
    texts = [format_job_text(r["title"], r["company_name"], r["location_raw"], r["description_text"], "local", 1500)[0]
             for r in rows]
    results: dict[str, dict] = {"baseline": baseline, "jobs": len(rows), "scored": n_scored}
    with OllamaClient(timeout=600) as client:
        for model in models:
            doc_prefix, query_prefix = PREFIXES.get(model, ("", ""))
            try:
                before = vram_mb()
                started = time.monotonic()
                vectors: list[list[float]] = []
                peak = before or 0
                for start in range(0, len(texts), 16):
                    batch = [doc_prefix + t for t in texts[start:start + 16]]
                    vectors += client.embed_batch(model, batch).vectors
                    used = vram_mb()
                    peak = max(peak, used or 0)
                elapsed = time.monotonic() - started
                job_vectors = unit(np.array(vectors, dtype="float64"))
                profile_vectors = unit(np.array(client.embed_batch(
                    model, [query_prefix + text for text in profiles.values()]).vectors, dtype="float64"))
                local_sim = (job_vectors @ profile_vectors.T).max(axis=1)
                row = {
                    "dims": int(job_vectors.shape[1]),
                    "jobs_per_second": round(len(texts) / elapsed, 1),
                    "seconds": round(elapsed, 1),
                    "vram_peak_mb": peak, "vram_before_mb": before,
                    "spearman_vs_gemini_embedding": spearman_or_nan(local_sim, gemini_sim),
                    "spearman_vs_fit": spearman_or_nan(local_sim[:n_scored], fit),
                    "top50_overlap_vs_fit": len(set(np.argsort(-local_sim[:n_scored])[:50]) & set(np.argsort(-fit)[:50])),
                    "top20_overlap_vs_fit": len(set(np.argsort(-local_sim[:n_scored])[:20]) & set(np.argsort(-fit)[:20])),
                }
                results[model] = row
                print(model, row)
                client.unload(model)
            except Exception as error:  # noqa: BLE001
                results[model] = {"error": f"{type(error).__name__}: {str(error)[:160]}"}
                print(model, results[model])
    out = ROOT / "data/eval/embedding_benchmark.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("wrote", out)


if __name__ == "__main__":
    main(sys.argv[1:])
