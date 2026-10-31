"""Feature extraction for the local triage cascade classifier (numpy only).

Extracts three feature groups:
1. Dense embeddings: stored 1024-d job embedding from `job_embeddings` (mxbai-embed-large), plus title
   and evidence extract embeddings computed via Ollama and cached in SQLite.
2. Hashed sparse n-grams: 2^16 (65,536) buckets of word 1-2-gram TF-IDF of title and evidence extract,
   plus char 3-5-grams of title. Stored in pure-numpy CSR format (no scipy).
3. Structured features: parsed years bounds, title level tokens, workplace type, country flags, source type,
   and description presence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jobhunt.config import Config
from jobhunt.matching.embeddings import pack, unpack
from jobhunt.matching.ollama import OllamaClient
from jobhunt.matching.triage import Evidence, build_evidence
from jobhunt.matching.years import required_years

logger = logging.getLogger(__name__)

HASH_BUCKETS = 65536  # 2^16
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#.]*[a-z0-9+#]|[a-z0-9]")
_STOPWORDS = frozenset(
    "a an and the of for to in at on with or is are be as by we you our your will this that it from".split()
)

LEVEL_TOKENS = (
    "intern", "internship", "junior", "entry", "fresher", "grad", "graduate",
    "mid", "senior", "sr", "lead", "principal", "staff", "director", "vp", "architect", "head"
)
WORKPLACE_TYPES = ("remote", "hybrid", "onsite", "unknown")
COUNTRY_CODES = ("IN", "US", "GB", "DE", "CA", "other")
SOURCE_TYPES = ("ats", "enterprise", "feed", "email", "other")


@dataclass
class SparseCSR:
    """Compressed Sparse Row matrix representation in pure numpy (no scipy)."""

    indptr: np.ndarray    # shape (N + 1,), dtype int32
    indices: np.ndarray   # shape (NNZ,), dtype int32
    data: np.ndarray      # shape (NNZ,), dtype float32
    shape: tuple[int, int]

    def dot_vector(self, x: np.ndarray) -> np.ndarray:
        """Matrix-vector product self @ x for x of shape (shape[1],)."""
        n_rows = self.shape[0]
        out = np.zeros(n_rows, dtype=np.float32)
        x_f32 = x.astype(np.float32, copy=False)
        for i in range(n_rows):
            start = self.indptr[i]
            end = self.indptr[i + 1]
            if start < end:
                out[i] = float(np.dot(self.data[start:end], x_f32[self.indices[start:end]]))
        return out

    def rdot_vector(self, y: np.ndarray) -> np.ndarray:
        """Transpose matrix-vector product self.T @ y for y of shape (shape[0],)."""
        n_cols = self.shape[1]
        out = np.zeros(n_cols, dtype=np.float32)
        y_f32 = y.astype(np.float32, copy=False)
        for i in range(self.shape[0]):
            start = self.indptr[i]
            end = self.indptr[i + 1]
            if start < end:
                val = y_f32[i]
                if val != 0.0:
                    out[self.indices[start:end]] += self.data[start:end] * val
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "indptr": self.indptr.tolist(),
            "indices": self.indices.tolist(),
            "data": self.data.tolist(),
            "shape": list(self.shape),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SparseCSR":
        return cls(
            indptr=np.array(d["indptr"], dtype=np.int32),
            indices=np.array(d["indices"], dtype=np.int32),
            data=np.array(d["data"], dtype=np.float32),
            shape=(d["shape"][0], d["shape"][1]),
        )


def _tokenize_words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS]


def extract_ngrams(title: str, extract: str) -> dict[int, float]:
    """Hashed word 1-2-grams of title and evidence, plus char 3-5-grams of title."""
    term_counts: dict[int, float] = {}

    # Title word 1-2 grams (higher weight)
    t_words = _tokenize_words(title)
    for w in t_words:
        h = zlib.crc32(f"t1:{w}".encode("utf-8")) % HASH_BUCKETS
        term_counts[h] = term_counts.get(h, 0.0) + 2.0
    for w1, w2 in zip(t_words, t_words[1:]):
        h = zlib.crc32(f"t2:{w1}_{w2}".encode("utf-8")) % HASH_BUCKETS
        term_counts[h] = term_counts.get(h, 0.0) + 3.0

    # Evidence extract word 1-2 grams
    e_words = _tokenize_words(extract)
    for w in e_words:
        h = zlib.crc32(f"e1:{w}".encode("utf-8")) % HASH_BUCKETS
        term_counts[h] = term_counts.get(h, 0.0) + 1.0
    for w1, w2 in zip(e_words, e_words[1:]):
        h = zlib.crc32(f"e2:{w1}_{w2}".encode("utf-8")) % HASH_BUCKETS
        term_counts[h] = term_counts.get(h, 0.0) + 1.5

    # Title char 3-5 grams (great for catching suffixes, seniority abbreviations like sr, jr)
    clean_title = (title or "").lower().strip()
    for n in (3, 4, 5):
        for i in range(len(clean_title) - n + 1):
            sub = clean_title[i : i + n]
            h = zlib.crc32(f"tc{n}:{sub}".encode("utf-8")) % HASH_BUCKETS
            term_counts[h] = term_counts.get(h, 0.0) + 1.0

    # L2 normalize term frequencies
    if term_counts:
        norm = np.sqrt(sum(v * v for v in term_counts.values()))
        if norm > 0:
            for k in term_counts:
                term_counts[k] /= norm

    return term_counts


def build_sparse_csr(rows_ngrams: list[dict[int, float]], n_cols: int = HASH_BUCKETS) -> SparseCSR:
    """Pack a list of {feature_index: value} into SparseCSR."""
    n_rows = len(rows_ngrams)
    indptr = [0]
    indices: list[int] = []
    data: list[float] = []

    for term_dict in rows_ngrams:
        sorted_items = sorted(term_dict.items())
        for idx, val in sorted_items:
            indices.append(idx)
            data.append(val)
        indptr.append(len(indices))

    return SparseCSR(
        indptr=np.array(indptr, dtype=np.int32),
        indices=np.array(indices, dtype=np.int32),
        data=np.array(data, dtype=np.float32),
        shape=(n_rows, n_cols),
    )


def extract_structured_features(row: sqlite3.Row, evidence: Evidence) -> list[float]:
    """Structured features: parsed years, title level tokens, workplace, location, source, missing description."""
    title_lower = (row["title"] or "").lower()
    desc = row["description_text"] or ""
    loc = (row["location_raw"] or "").lower()
    country = (row["country"] or "").upper()
    workplace = (row["workplace_type"] or "unknown").lower()
    sources = (row["source_name"] or "").lower() if "source_name" in row.keys() else ""

    # 1. Parsed years
    years_val, years_phrase = required_years(desc, title=row["title"])
    y_min = float(years_val) if years_val is not None else 0.0
    y_max = y_min
    has_years = 1.0 if years_val is not None else 0.0

    # 2. Level tokens in title
    level_flags = [1.0 if t in title_lower else 0.0 for t in LEVEL_TOKENS]

    # 3. Workplace flags
    workplace_flags = [1.0 if workplace == w else 0.0 for w in WORKPLACE_TYPES]

    # 4. Country flags
    matched_country = country if country in COUNTRY_CODES[:-1] else "other"
    country_flags = [1.0 if matched_country == c else 0.0 for c in COUNTRY_CODES]

    # 5. Missing description
    missing_desc = 1.0 if not desc.strip() else 0.0

    # 6. Source kind
    s_kind = "other"
    if any(s in sources for s in ("workday", "amazon", "oracle")):
        s_kind = "enterprise"
    elif any(s in sources for s in ("greenhouse", "lever", "ashby", "workable")):
        s_kind = "ats"
    elif "email" in sources:
        s_kind = "email"
    elif sources:
        s_kind = "feed"
    source_flags = [1.0 if s_kind == s else 0.0 for s in SOURCE_TYPES]

    return [y_min, y_max, has_years, missing_desc, *level_flags, *workplace_flags, *country_flags, *source_flags]


STRUCTURED_DIM = len(
    extract_structured_features(
        # Dummy row
        {"title": "", "description_text": "", "location_raw": "", "country": "", "workplace_type": "", "source_name": ""},  # type: ignore[arg-type]
        Evidence(title="", company="", location="", workplace="", extract=""),
    )
)


class EmbeddingCache:
    """Caches title and evidence extract embeddings in SQLite to run free and fast."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self._ensure_table()

    def _ensure_table(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS triage_embedding_cache (
                text_hash TEXT PRIMARY KEY,
                vector BLOB NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

    def get(self, text: str, model: str) -> np.ndarray | None:
        digest = hashlib.sha256(f"{model}:{text}".encode("utf-8")).hexdigest()
        row = self.conn.execute(
            "SELECT vector FROM triage_embedding_cache WHERE text_hash = ? AND model = ?", (digest, model)
        ).fetchone()
        if row is not None:
            return unpack(row["vector"])
        return None

    def put_batch(self, items: list[tuple[str, str, np.ndarray]], now_iso: str) -> None:
        from jobhunt.db import transaction

        rows = []
        for text, model, vec in items:
            digest = hashlib.sha256(f"{model}:{text}".encode("utf-8")).hexdigest()
            rows.append((digest, pack(vec.tolist()), model, now_iso))
        if rows:
            with transaction(self.conn):
                self.conn.executemany(
                    "INSERT OR REPLACE INTO triage_embedding_cache (text_hash, vector, model, created_at) VALUES (?, ?, ?, ?)",
                    rows,
                )


def load_job_embeddings(connection: sqlite3.Connection, job_ids: list[int]) -> dict[int, np.ndarray]:
    """Retrieve stored 1024-d job embeddings from job_embeddings table."""
    if not job_ids:
        return {}
    out: dict[int, np.ndarray] = {}
    placeholders = ",".join("?" for _ in job_ids)
    query = f"""
        SELECT job_id, vector FROM job_embeddings
        WHERE job_id IN ({placeholders}) AND model = 'ollama:mxbai-embed-large'
    """
    for row in connection.execute(query, job_ids):
        out[int(row["job_id"])] = unpack(row["vector"]).astype(np.float32)
    return out


def ensure_text_embeddings(
    connection: sqlite3.Connection,
    texts: list[str],
    model: str = "mxbai-embed-large",
    client: OllamaClient | None = None,
    now_iso: str = "2026-10-30T00:00:00Z",
) -> list[np.ndarray]:
    """Retrieve embeddings for texts from cache or compute once via Ollama."""
    cache = EmbeddingCache(connection)
    results: list[np.ndarray | None] = [cache.get(t, model) for t in texts]

    missing_indexes = [i for i, v in enumerate(results) if v is None]
    if missing_indexes and client is not None and client.available():
        missing_texts = [texts[i] for i in missing_indexes]
        batch_size = 32
        new_cached: list[tuple[str, str, np.ndarray]] = []
        for start in range(0, len(missing_texts), batch_size):
            chunk = missing_texts[start : start + batch_size]
            try:
                res = client.embed_batch(model, chunk)
                for j, vec in enumerate(res.vectors):
                    arr = np.array(vec, dtype=np.float32)
                    idx = missing_indexes[start + j]
                    results[idx] = arr
                    new_cached.append((texts[idx], model, arr))
            except Exception as err:
                logger.warning("Failed to embed text chunk with Ollama: %s", err)
                break
        if new_cached:
            cache.put_batch(new_cached, now_iso)

    # Fallback to zeros for any that could not be embedded
    dim = 1024
    out: list[np.ndarray] = []
    for v in results:
        if v is not None and len(v) == dim:
            out.append(v)
        else:
            out.append(np.zeros(dim, dtype=np.float32))
    return out


@dataclass
class ExtractedDataset:
    job_ids: list[int]
    companies: list[str]
    created_dates: list[str]
    dense_features: np.ndarray       # shape (N, D_dense)
    sparse_features: SparseCSR       # shape (N, HASH_BUCKETS)


def extract_features_for_jobs(
    connection: sqlite3.Connection,
    job_ids: list[int],
    *,
    client: OllamaClient | None = None,
    now_iso: str = "2026-10-30T00:00:00Z",
) -> ExtractedDataset:
    """Extract dense and sparse features for a list of job ids."""
    if not job_ids:
        return ExtractedDataset([], [], [], np.zeros((0, 0), dtype=np.float32), SparseCSR(np.array([0]), np.array([]), np.array([]), (0, HASH_BUCKETS)))

    placeholders = ",".join("?" for _ in job_ids)
    query = f"""
        SELECT id, title, company_name, location_raw, country, workplace_type, remote_regions, description_text, first_seen_at
        FROM jobs
        WHERE id IN ({placeholders})
    """
    rows_by_id = {int(r["id"]): r for r in connection.execute(query, job_ids)}

    ordered_jobs = [jid for jid in job_ids if jid in rows_by_id]
    N = len(ordered_jobs)
    if N == 0:
        return ExtractedDataset([], [], [], np.zeros((0, 0), dtype=np.float32), SparseCSR(np.array([0]), np.array([]), np.array([]), (0, HASH_BUCKETS)))

    # Build evidence and text objects
    evidences = [
        build_evidence(
            rows_by_id[jid]["title"] or "",
            rows_by_id[jid]["company_name"] or "",
            rows_by_id[jid]["location_raw"],
            rows_by_id[jid]["workplace_type"],
            rows_by_id[jid]["remote_regions"],
            rows_by_id[jid]["description_text"],
        )
        for jid in ordered_jobs
    ]

    # Stored 1024-d job embeddings
    job_embs = load_job_embeddings(connection, ordered_jobs)

    # Title & Extract embeddings (Ollama mxbai-embed-large, cached)
    titles = [rows_by_id[jid]["title"] or "" for jid in ordered_jobs]
    extracts = [ev.extract for ev in evidences]
    title_embs = ensure_text_embeddings(connection, titles, "mxbai-embed-large", client=client, now_iso=now_iso)
    extract_embs = ensure_text_embeddings(connection, extracts, "mxbai-embed-large", client=client, now_iso=now_iso)

    # Structured features
    structured_list = [extract_structured_features(rows_by_id[jid], evidences[i]) for i, jid in enumerate(ordered_jobs)]
    structured_arr = np.array(structured_list, dtype=np.float32)

    # Assemble dense feature matrix: [job_emb(1024), title_emb(1024), extract_emb(1024), structured(dim)]
    dense_dim = 1024 + 1024 + 1024 + STRUCTURED_DIM
    dense_matrix = np.zeros((N, dense_dim), dtype=np.float32)
    for i, jid in enumerate(ordered_jobs):
        j_vec = job_embs.get(jid, np.zeros(1024, dtype=np.float32))
        dense_matrix[i, 0:1024] = j_vec
        dense_matrix[i, 1024:2048] = title_embs[i]
        dense_matrix[i, 2048:3072] = extract_embs[i]
        dense_matrix[i, 3072:] = structured_arr[i]

    # Sparse n-gram features
    ngram_dicts = [_extract_ngrams(rows_by_id[jid]["title"] or "", evidences[i].extract) for i, jid in enumerate(ordered_jobs)]
    sparse_csr = build_sparse_csr(ngram_dicts, n_cols=HASH_BUCKETS)

    companies = [rows_by_id[jid]["company_name"] or "unknown" for jid in ordered_jobs]
    created_dates = [rows_by_id[jid]["first_seen_at"] or "" for jid in ordered_jobs]

    return ExtractedDataset(
        job_ids=ordered_jobs,
        companies=companies,
        created_dates=created_dates,
        dense_features=dense_matrix,
        sparse_features=sparse_csr,
    )


_extract_ngrams = extract_ngrams

__all__ = [
    "HASH_BUCKETS",
    "STRUCTURED_DIM",
    "EmbeddingCache",
    "ExtractedDataset",
    "SparseCSR",
    "build_sparse_csr",
    "ensure_text_embeddings",
    "extract_features_for_jobs",
    "extract_ngrams",
    "extract_structured_features",
    "load_job_embeddings",
]
