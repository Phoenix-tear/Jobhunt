"""Embeddings and similarity: profiles and open target/maybe jobs -> vectors -> cosine scores.

Rules (see the step-3 brief):
* only jobs that are open and judged `target` or `maybe` are embedded;
* a stored vector is reused only when (job, model, dimensions, prompt version, text hash) all match,
  so a changed description, a new model, a new size or a new prompt format re-embeds;
* one vector per input: `batchEmbedContents` with one request per text (never several parts in one
  content, which `gemini-embedding-2` would merge into a single vector);
* similarity is one numpy matrix product over the whole table (seconds for 20k jobs).

Spring Boot analogy: `embed_jobs` is a `@Scheduled` batch job with a cache (Spring Cache keyed by
`(job, model, dimensions, hash)`); `compute_similarity` is a pure repository-side aggregation.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from jobhunt.config import Config
from jobhunt.matching.budget import BudgetGuard, UsageLedger, estimate_cost, estimate_tokens
from jobhunt.matching.gemini import GeminiClient, GeminiError, GeminiQuotaError, normalize
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")
_CHARS_PER_TOKEN = 4
_EMBEDDING_2_PREFIX = "gemini-embedding-2"


# --- text formats -----------------------------------------------------------------------------


def effective_config(config: Config) -> Config:
    """The config the embedding code should use: for `provider: local` or `ollama` the model is `ollama:<name>` at the
    local vector size, with no per-day text cap (it is free). For `provider: none`, embeddings are disabled."""
    import dataclasses

    settings = config.embedding
    provider = (settings.provider or "ollama").lower()
    if provider in ("local", "ollama"):
        local = dataclasses.replace(
            settings,
            provider="ollama",
            model=f"ollama:{settings.local_model}",
            dimensions=settings.local_dimensions,
            batch_size=min(settings.batch_size, 32),
            max_concurrent_batches=1,
            max_tokens_per_minute=10**9,
        )
        budget = dataclasses.replace(config.budget, daily_embedding_texts=10**9)
        return dataclasses.replace(config, embedding=local, budget=budget)
    elif provider == "none":
        none_cfg = dataclasses.replace(settings, provider="none")
        return dataclasses.replace(config, embedding=none_cfg)
    return config


def uses_prompt_prefixes(model: str) -> bool:
    """embedding-2 has no task_type parameter: the task goes into the text itself."""
    return model.startswith(_EMBEDDING_2_PREFIX)


def format_profile_text(text: str, model: str) -> tuple[str, str | None]:
    """(text to embed, task_type) for a profile, which plays the QUERY role."""
    if uses_prompt_prefixes(model):
        return f"task: search result | query: {text}", None
    return text, "RETRIEVAL_QUERY"


def truncate_description(description: str | None, token_limit: int) -> str:
    cleaned = _WHITESPACE.sub(" ", description or "").strip()
    limit = max(1, token_limit) * _CHARS_PER_TOKEN
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > limit * 0.8 else cut).rstrip()


def format_job_text(
    title: str, company: str, location: str | None, description: str | None, model: str, token_limit: int
) -> tuple[str, str | None]:
    """(text to embed, task_type) for a job, which plays the DOCUMENT role."""
    body = " | ".join(part for part in (
        company.strip(), (location or "").strip(), truncate_description(description, token_limit)) if part)
    if uses_prompt_prefixes(model):
        return f"title: {title.strip() or 'none'} | text: {body}", None
    return f"{title.strip()}\n{body}", "RETRIEVAL_DOCUMENT"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pack(vector: list[float]) -> bytes:
    return np.asarray(vector, dtype="<f4").tobytes()


def unpack(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")


def finalize_vector(vector: list[float], model: str) -> list[float]:
    """embedding-2 returns unit vectors for truncated sizes; others must be normalized by hand."""
    norm = float(np.linalg.norm(np.asarray(vector, dtype="float64")))
    if uses_prompt_prefixes(model) and abs(norm - 1.0) < 1e-3:
        return vector
    return normalize(vector)


class TokenPacer:
    """Keeps tokens-per-minute under a self-imposed limit: sleeps before a wave that would exceed it."""

    def __init__(self, tokens_per_minute: int, *, sleep=None, monotonic=None) -> None:  # noqa: ANN001
        import time

        self._limit = max(1, tokens_per_minute)
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._events: list[tuple[float, int]] = []   # (time, tokens) inside the last minute

    def wait_for(self, tokens: int) -> float:
        """Block until `tokens` fit into the current minute; returns the seconds slept."""
        slept = 0.0
        tokens = min(tokens, self._limit)             # a single wave larger than the limit still runs
        while True:
            now = self._monotonic()
            self._events = [(when, count) for when, count in self._events if now - when < 60.0]
            used = sum(count for _, count in self._events)
            if used + tokens <= self._limit or not self._events:
                self._events.append((now, tokens))
                return slept
            delay = 60.0 - (now - self._events[0][0]) + 0.05
            self._sleep(delay)
            slept += delay


# --- profiles ---------------------------------------------------------------------------------


def embed_profiles(
    connection: sqlite3.Connection, config: Config, client: GeminiClient, ledger: UsageLedger, guard: BudgetGuard,
    *, clock: Clock = utc_now,
) -> int:
    """Embed every stored profile whose text / model / size / prompt format changed. Returns how many."""
    settings = config.embedding
    done = 0
    for row in connection.execute("SELECT name, stripped_text FROM profiles ORDER BY name").fetchall():
        text, task_type = format_profile_text(row["stripped_text"], settings.model)
        digest = text_hash(text)
        cached = connection.execute(
            """SELECT 1 FROM profile_embeddings WHERE profile_name = ? AND model = ? AND dimensions = ?
               AND prompt_version = ? AND text_hash = ?""",
            (row["name"], settings.model, settings.dimensions, settings.prompt_version, digest),
        ).fetchone()
        if cached:
            continue
        tokens = estimate_tokens(text)
        decision = guard.check_embed(1, estimate_cost(config.pricing, settings.model, tokens, 0, clock().date()))
        if not decision:
            raise GeminiQuotaError(f"budget: {decision.reason}")
        result = client.embed_batch(settings.model, [text], dimensions=settings.dimensions, task_type=task_type)
        vector = finalize_vector(result.vectors[0], settings.model)
        connection.execute("BEGIN IMMEDIATE")
        try:
            provider_val = "ollama" if settings.model.startswith("ollama:") else "gemini"
            connection.execute(
                """INSERT INTO profile_embeddings (profile_name, model, dimensions, prompt_version, text_hash, vector,
                                                   created_at, provider)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(profile_name, model) DO UPDATE SET dimensions = excluded.dimensions,
                       prompt_version = excluded.prompt_version, text_hash = excluded.text_hash,
                       vector = excluded.vector, created_at = excluded.created_at,
                       provider = excluded.provider""",
                (row["name"], settings.model, settings.dimensions, settings.prompt_version, digest, pack(vector),
                 to_iso(clock()), provider_val),
            )
            ledger.record(settings.model, "embed", input_tokens=result.input_tokens or tokens, items=1,
                          note=f"profile:{row['name']}")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
        done += 1
    return done


# --- jobs -------------------------------------------------------------------------------------


@dataclass
class EmbedStats:
    candidates: int = 0
    cached: int = 0
    to_embed: int = 0
    embedded: int = 0
    failed: int = 0
    batches: int = 0
    tokens: int = 0
    estimated_usd: float = 0.0
    stopped_reason: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Pending:
    job_id: int
    text: str
    task_type: str | None
    digest: str
    description_hash: str | None
    tokens: int


def collect_pending(
    connection: sqlite3.Connection, config: Config, *, limit: int | None = None
) -> tuple[list[_Pending], EmbedStats]:
    """Open target/maybe jobs whose stored vector is missing or no longer valid (targets first)."""
    cfg = effective_config(config)
    settings = cfg.embedding
    if settings.provider == "none":
        return [], EmbedStats(stopped_reason="Embeddings provider is 'none' (similarity disabled)")
    stats = EmbedStats()
    pending: list[_Pending] = []
    cursor = connection.execute(
        """SELECT j.id, j.title, j.company_name, j.location_raw, j.description_text, j.description_hash,
                  e.dimensions AS e_dims, e.prompt_version AS e_prompt, e.text_hash AS e_hash
           FROM jobs j
           JOIN job_relevance r ON r.job_id = j.id
           LEFT JOIN job_embeddings e ON e.job_id = j.id AND e.model = ?
           WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe')
           ORDER BY (r.gate_status = 'target') DESC, j.first_seen_at DESC, j.id DESC""",
        (settings.model,),
    )
    for row in cursor:
        stats.candidates += 1
        text, task_type = format_job_text(
            row["title"], row["company_name"], row["location_raw"], row["description_text"],
            settings.model, settings.description_token_limit,
        )
        digest = text_hash(text)
        if row["e_hash"] == digest and row["e_dims"] == settings.dimensions and row["e_prompt"] == settings.prompt_version:
            stats.cached += 1
            continue
        if limit is not None and len(pending) >= limit:
            continue
        pending.append(_Pending(int(row["id"]), text, task_type, digest, row["description_hash"], estimate_tokens(text)))
    stats.to_embed = len(pending)
    stats.tokens = sum(item.tokens for item in pending)
    return pending, stats


def embed_jobs(
    connection: sqlite3.Connection, config: Config, client: GeminiClient, ledger: UsageLedger, guard: BudgetGuard,
    *, limit: int | None = None, clock: Clock = utc_now, pacer: "TokenPacer | None" = None,
) -> EmbedStats:
    """Embed what `collect_pending` found, in concurrent batches, with budget checks and isolation."""
    cfg = effective_config(config)
    settings = cfg.embedding
    if settings.provider == "none":
        return EmbedStats(stopped_reason="Embeddings provider is 'none' (similarity disabled)")
    pending, stats = collect_pending(connection, config, limit=limit)
    if not pending:
        return stats
    price_day = clock().date()
    pacer = pacer or TokenPacer(settings.max_tokens_per_minute)
    batches = [pending[i:i + settings.batch_size] for i in range(0, len(pending), settings.batch_size)]
    wave_size = max(1, settings.max_concurrent_batches)

    def run(batch: list[_Pending]) -> tuple[list[_Pending], Any, GeminiError | None]:
        try:
            result = client.embed_batch(
                settings.model, [item.text for item in batch], dimensions=settings.dimensions,
                task_type=batch[0].task_type,
            )
            return batch, result, None
        except GeminiError as error:
            return batch, None, error

    for start in range(0, len(batches), wave_size):
        wave = batches[start:start + wave_size]
        wave_texts = sum(len(batch) for batch in wave)
        wave_tokens = sum(item.tokens for batch in wave for item in batch)
        decision = guard.check_embed(wave_texts, estimate_cost(config.pricing, settings.model, wave_tokens, 0, price_day))
        if not decision:
            stats.stopped_reason = decision.reason
            break
        pacer.wait_for(wave_tokens)
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            outcomes = list(pool.map(run, wave))

        quota_hit = False
        for batch, result, error in outcomes:
            stats.batches += 1
            if error is not None:
                stats.failed += len(batch)
                stats.errors.append(str(error))
                quota_hit = quota_hit or isinstance(error, GeminiQuotaError)
                logger.warning("embedding batch of %d failed: %s", len(batch), error)
                continue
            now_iso = to_iso(clock())
            provider_val = "ollama" if settings.model.startswith("ollama:") else "gemini"
            rows = []
            for item, vector in zip(batch, result.vectors, strict=True):
                final = finalize_vector(vector, settings.model)
                rows.append((item.job_id, settings.model, pack(final), item.description_hash, now_iso,
                             len(final), settings.prompt_version, item.digest, item.tokens, provider_val))
            tokens = result.input_tokens or sum(item.tokens for item in batch)
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    """INSERT INTO job_embeddings (job_id, model, vector, description_hash, created_at, dimensions,
                                                   prompt_version, text_hash, token_estimate, provider)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(job_id, model) DO UPDATE SET vector = excluded.vector,
                           description_hash = excluded.description_hash, created_at = excluded.created_at,
                           dimensions = excluded.dimensions, prompt_version = excluded.prompt_version,
                           text_hash = excluded.text_hash, token_estimate = excluded.token_estimate,
                           provider = excluded.provider""",
                    rows,
                )
                connection.executemany(
                    "UPDATE jobs SET embedded_at = ? WHERE id = ?", [(now_iso, item.job_id) for item in batch]
                )
                stats.estimated_usd += ledger.record(settings.model, "embed", input_tokens=tokens, items=len(batch),
                                                     note=f"{len(batch)} jobs")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
            stats.embedded += len(batch)
        if quota_hit:
            stats.stopped_reason = "rate limited by the API (429) after retries"
            break
    return stats


# --- similarity -------------------------------------------------------------------------------


@dataclass
class SimilarityStats:
    jobs_scored: int = 0
    profiles: list[str] = field(default_factory=list)
    best_profile_counts: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0


def compute_similarity(connection: sqlite3.Connection, config: Config, *, clock: Clock = utc_now) -> SimilarityStats:
    """Cosine of every embedded open job against every profile; best profile and percentile stored."""
    import time

    started = time.perf_counter()
    cfg = effective_config(config)
    settings = cfg.embedding
    stats = SimilarityStats()

    if settings.provider == "none":
        stats.seconds = time.perf_counter() - started
        return stats

    # Refuse mixing across different dimensions or models
    try:
        stored_prof_dims = [
            r["dimensions"] for r in connection.execute("SELECT DISTINCT dimensions FROM profile_embeddings").fetchall()
        ]
        if stored_prof_dims and any(d != settings.dimensions for d in stored_prof_dims):
            raise ValueError(
                f"Embedding vector dimension mismatch: stored profile embeddings are {stored_prof_dims}d "
                f"while current config uses {settings.dimensions}d ({settings.model}). Mixing is refused; recompute embeddings."
            )
        stored_job_dims = [
            r["dimensions"] for r in connection.execute("SELECT DISTINCT dimensions FROM job_embeddings").fetchall()
        ]
        if stored_job_dims and any(d != settings.dimensions for d in stored_job_dims):
            raise ValueError(
                f"Embedding vector dimension mismatch: stored job embeddings are {stored_job_dims}d "
                f"while current config uses {settings.dimensions}d ({settings.model}). Mixing is refused; recompute embeddings."
            )
    except sqlite3.OperationalError:
        pass

    try:
        profile_rows = connection.execute(
            """SELECT base_id AS profile_name, vector FROM profile_embeddings_v2 WHERE model = ? AND dimensions = ?
               ORDER BY base_id""", (settings.model, settings.dimensions),
        ).fetchall()
    except sqlite3.OperationalError:
        profile_rows = []
    if not profile_rows:
        profile_rows = connection.execute(
            """SELECT profile_name, vector FROM profile_embeddings WHERE model = ? AND dimensions = ?
               ORDER BY profile_name""", (settings.model, settings.dimensions),
        ).fetchall()
    if not profile_rows:
        return stats
    names = [row["profile_name"] for row in profile_rows]
    profile_matrix = np.vstack([unpack(row["vector"]) for row in profile_rows]).astype("float32")
    if profile_matrix.shape[1] != settings.dimensions:
        raise ValueError(
            f"Embedding profile vector dimension mismatch: profiles are {profile_matrix.shape[1]}d "
            f"while config specifies {settings.dimensions}d. Mixing is refused; recompute embeddings."
        )
    profile_matrix /= np.linalg.norm(profile_matrix, axis=1, keepdims=True)

    rows = connection.execute(
        """SELECT e.job_id, e.vector FROM job_embeddings e
           JOIN job_relevance r ON r.job_id = e.job_id JOIN jobs j ON j.id = e.job_id
           WHERE j.status = 'open' AND e.model = ? AND e.dimensions = ?""",
        (settings.model, settings.dimensions),
    ).fetchall()
    if not rows:
        stats.profiles = names
        return stats

    job_ids = [int(row["job_id"]) for row in rows]
    matrix = np.vstack([unpack(row["vector"]) for row in rows]).astype("float32")
    if matrix.shape[1] != settings.dimensions:
        raise ValueError(
            f"Embedding job vector dimension mismatch: jobs are {matrix.shape[1]}d "
            f"while config specifies {settings.dimensions}d. Mixing is refused; recompute embeddings."
        )
    if profile_matrix.shape[1] != matrix.shape[1]:
        raise ValueError(
            f"Embedding vector dimension mismatch: profiles are {profile_matrix.shape[1]}d "
            f"while jobs are {matrix.shape[1]}d. Mixing is refused; recompute embeddings."
        )
    matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
    scores = matrix @ profile_matrix.T                               # (jobs, profiles)

    from jobhunt.profile import get_profile

    p = get_profile(config)
    authoritative = p.authoritative_base if p else config.profiles.authoritative
    preference = np.array([0 if name == authoritative else 1 for name in names])
    best_index = np.argmax(scores - preference * 1e-9, axis=1)       # ties go to the authoritative resume
    best_score = scores[np.arange(len(job_ids)), best_index]
    order = best_score.argsort().argsort()                            # rank 0..n-1
    percentile = order / max(1, len(job_ids) - 1)

    column = {name: scores[:, i] for i, name in enumerate(names)}
    updates: list[tuple[Any, ...]] = []
    v2_updates: list[tuple[Any, ...]] = []
    now_iso = to_iso(clock())
    for position, job_id in enumerate(job_ids):
        best_name = names[int(best_index[position])]
        stats.best_profile_counts[best_name] = stats.best_profile_counts.get(best_name, 0) + 1
        updates.append((
            float(column["ai"][position]) if "ai" in column else None,
            float(column["fullstack"][position]) if "fullstack" in column else None,
            best_name, float(percentile[position]), job_id,
        ))
        for name in names:
            v2_updates.append((job_id, name, float(column[name][position]), now_iso))
    connection.execute("BEGIN IMMEDIATE")
    try:
        try:
            connection.executemany(
                """INSERT INTO job_base_similarity_v2 (job_id, base_id, similarity, computed_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(job_id, base_id) DO UPDATE SET similarity = excluded.similarity,
                       computed_at = excluded.computed_at""",
                v2_updates,
            )
        except sqlite3.OperationalError:
            pass
        connection.executemany(
            """UPDATE job_relevance SET similarity_ai = ?, similarity_fullstack = ?,
                      best_profile = CASE WHEN llm_status = 'scored' THEN best_profile ELSE ? END,
                      similarity_percentile = ? WHERE job_id = ?""",
            updates,
        )
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
    stats.jobs_scored = len(updates)
    stats.profiles = names
    stats.seconds = time.perf_counter() - started
    return stats


def estimate_recompute_cost(
    connection: sqlite3.Connection,
    config: Config,
    target_provider: str,
    target_model: str | None = None,
) -> dict[str, Any]:
    """Estimate token volume and USD cost to recompute all embeddings for target provider."""
    provider = target_provider.lower()
    cursor = connection.execute(
        """SELECT j.id, j.title, j.company_name, j.location_raw, j.description_text
           FROM jobs j
           JOIN job_relevance r ON r.job_id = j.id
           WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe')"""
    )
    job_rows = cursor.fetchall()
    job_count = len(job_rows)

    if provider in ("none", "ollama", "local"):
        return {
            "provider": provider,
            "job_count": job_count,
            "estimated_tokens": 0,
            "estimated_usd": 0.0,
            "free": True,
        }

    # Gemini
    model = target_model or config.embedding.model
    token_limit = config.embedding.description_token_limit
    total_tokens = 0
    for row in job_rows:
        text, _ = format_job_text(
            row["title"], row["company_name"], row["location_raw"], row["description_text"],
            model, token_limit,
        )
        total_tokens += estimate_tokens(text)

    # Add profile tokens
    for prow in connection.execute("SELECT stripped_text FROM profiles").fetchall():
        ptext, _ = format_profile_text(prow["stripped_text"], model)
        total_tokens += estimate_tokens(ptext)

    cost = estimate_cost(config.pricing, model, total_tokens, 0, utc_now().date())
    return {
        "provider": "gemini",
        "model": model,
        "job_count": job_count,
        "estimated_tokens": total_tokens,
        "estimated_usd": round(cost, 4),
        "pricing_note": "See Google's current pricing for text-embedding models (tokens computed dynamically; not a guaranteed price).",
        "free": False,
    }

