"""Tests for optional embeddings (ollama, gemini, none), dimension-mismatch protection, and cost estimation (Brief v13)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from jobhunt.config import Config, EmbeddingSettings, ScoringSettings
from jobhunt.db import connect
from jobhunt.matching.budget import BudgetGuard, UsageLedger
from jobhunt.matching.embeddings import (
    EmbedStats,
    compute_similarity,
    effective_config,
    embed_jobs,
    estimate_recompute_cost,
    pack,
)
from jobhunt.matching.gemini import EmbedResult
from jobhunt.matching.scoring import compute_final_scores


@pytest.fixture
def embed_db(tmp_path: Path) -> tuple[sqlite3.Connection, Config]:
    db_path = tmp_path / "data" / "jobhunt.sqlite3"
    db_path.parent.mkdir(parents=True)
    conn = connect(db_path)

    # Setup necessary tables
    conn.execute(
        """CREATE TABLE jobs (
               id INTEGER PRIMARY KEY,
               title TEXT NOT NULL,
               company_name TEXT NOT NULL,
               location_raw TEXT,
               description_text TEXT,
               description_hash TEXT,
               status TEXT NOT NULL DEFAULT 'open',
               first_seen_at TEXT,
               embedded_at TEXT,
               country TEXT,
               workplace_type TEXT,
               remote_eligible_india INTEGER DEFAULT 0
           )"""
    )
    conn.execute(
        """CREATE TABLE job_relevance (
               job_id INTEGER PRIMARY KEY,
               gate_status TEXT NOT NULL,
               similarity_ai REAL,
               similarity_fullstack REAL,
               similarity_percentile REAL,
               llm_score REAL,
               llm_status TEXT,
               llm_flags TEXT,
               rank_adjust REAL DEFAULT 0,
               required_years REAL,
               seniority_level TEXT,
               local_rank_score REAL,
               location_fit REAL,
               eligibility_json TEXT,
               learned_json TEXT,
               final_score REAL,
               score_components TEXT,
               best_profile TEXT
           )"""
    )
    conn.execute(
        """CREATE TABLE job_embeddings (
               job_id INTEGER,
               model TEXT,
               vector BLOB,
               description_hash TEXT,
               created_at TEXT,
               dimensions INTEGER,
               prompt_version TEXT,
               text_hash TEXT,
               token_estimate INTEGER,
               provider TEXT,
               PRIMARY KEY (job_id, model)
           )"""
    )
    conn.execute(
        """CREATE TABLE profile_embeddings (
               profile_name TEXT,
               model TEXT,
               dimensions INTEGER,
               prompt_version TEXT,
               text_hash TEXT,
               vector BLOB,
               created_at TEXT,
               provider TEXT,
               PRIMARY KEY (profile_name, model)
           )"""
    )
    conn.execute(
        """CREATE TABLE profiles (
               name TEXT PRIMARY KEY,
               stripped_text TEXT
           )"""
    )
    conn.commit()

    config = Config(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_path=str(db_path),
        embedding=EmbeddingSettings(
            provider="gemini",
            model="gemini-embedding-2",
            dimensions=768,
        ),
        scoring=ScoringSettings(
            weight_fit=0.50,
            weight_similarity_percentile=0.30,
            weight_location=0.20,
        ),
    )
    return conn, config


class TestEmbeddingsProvider:
    def test_effective_config_providers(self, embed_db: tuple[sqlite3.Connection, Config]) -> None:
        _, config = embed_db

        # Test 'gemini'
        c_gemini = effective_config(replace(config, embedding=replace(config.embedding, provider="gemini")))
        assert c_gemini.embedding.provider == "gemini"
        assert c_gemini.embedding.model == "gemini-embedding-2"

        # Test 'ollama' / 'local'
        c_ollama = effective_config(replace(config, embedding=replace(config.embedding, provider="ollama", local_model="mxbai-embed-large", local_dimensions=1024)))
        assert c_ollama.embedding.provider == "ollama"
        assert c_ollama.embedding.model == "ollama:mxbai-embed-large"
        assert c_ollama.embedding.dimensions == 1024

        # Test 'none'
        c_none = effective_config(replace(config, embedding=replace(config.embedding, provider="none")))
        assert c_none.embedding.provider == "none"

    def test_gemini_embed_jobs_mock_client(self, embed_db: tuple[sqlite3.Connection, Config]) -> None:
        conn, config = embed_db

        # Insert candidate jobs
        conn.execute("INSERT INTO jobs (id, title, company_name, description_text, status) VALUES (1, 'Software Engineer', 'Acme', 'Python developer', 'open')")
        conn.execute("INSERT INTO job_relevance (job_id, gate_status) VALUES (1, 'target')")
        conn.commit()

        mock_vector = [0.1] * 768
        mock_result = EmbedResult(vectors=[mock_vector], input_tokens=15)
        mock_client = MagicMock()
        mock_client.embed_batch.return_value = mock_result
        mock_ledger = MagicMock()
        mock_ledger.record.return_value = 0.0001
        mock_guard = MagicMock()
        mock_guard.check_embed.return_value = MagicMock(allowed=True)

        stats = embed_jobs(conn, config, mock_client, mock_ledger, mock_guard)
        assert stats.embedded == 1
        assert stats.failed == 0

        # Check DB entry
        row = conn.execute("SELECT model, dimensions, provider FROM job_embeddings WHERE job_id = 1").fetchone()
        assert row is not None
        assert row["model"] == "gemini-embedding-2"
        assert row["dimensions"] == 768
        assert row["provider"] == "gemini"

    def test_provider_none_skips_embedding(self, embed_db: tuple[sqlite3.Connection, Config]) -> None:
        conn, config = embed_db
        none_config = replace(config, embedding=replace(config.embedding, provider="none"))

        conn.execute("INSERT INTO jobs (id, title, company_name, description_text, status) VALUES (2, 'SWE', 'Acme', 'Rust developer', 'open')")
        conn.execute("INSERT INTO job_relevance (job_id, gate_status) VALUES (2, 'target')")
        conn.commit()

        mock_client = MagicMock()
        mock_ledger = MagicMock()
        mock_guard = MagicMock()

        stats = embed_jobs(conn, none_config, mock_client, mock_ledger, mock_guard)
        assert stats.embedded == 0
        assert "none" in (stats.stopped_reason or "")
        mock_client.embed_batch.assert_not_called()

    def test_dimension_mismatch_refusal(self, embed_db: tuple[sqlite3.Connection, Config]) -> None:
        conn, config = embed_db

        # Store profile with 1024d vector (e.g. from Ollama)
        p_vec = pack([0.1] * 1024)
        conn.execute(
            "INSERT INTO profile_embeddings (profile_name, model, dimensions, vector) VALUES ('default', 'ollama:mxbai-embed-large', 1024, ?)",
            (p_vec,),
        )

        # Store job with 768d vector (e.g. from Gemini)
        j_vec = pack([0.1] * 768)
        conn.execute("INSERT INTO jobs (id, title, company_name, status) VALUES (10, 'Engineer', 'Beta', 'open')")
        conn.execute("INSERT INTO job_relevance (job_id, gate_status) VALUES (10, 'target')")
        conn.execute(
            "INSERT INTO job_embeddings (job_id, model, dimensions, vector) VALUES (10, 'gemini-embedding-2', 768, ?)",
            (j_vec,),
        )
        conn.commit()

        # When running with 768d config, profile dimension mismatch is refused
        with pytest.raises(ValueError, match="dimension mismatch"):
            compute_similarity(conn, config)

    def test_cost_estimate_gemini_vs_ollama(self, embed_db: tuple[sqlite3.Connection, Config]) -> None:
        conn, config = embed_db

        conn.execute("INSERT INTO jobs (id, title, company_name, description_text, status) VALUES (1, 'ML Eng', 'AI Corp', 'PyTorch deep learning', 'open')")
        conn.execute("INSERT INTO job_relevance (job_id, gate_status) VALUES (1, 'target')")
        conn.execute("INSERT INTO profiles (name, stripped_text) VALUES ('main', 'Senior ML engineer')")
        conn.commit()

        # Ollama cost estimate should be $0
        ollama_est = estimate_recompute_cost(conn, config, "ollama")
        assert ollama_est["job_count"] == 1
        assert ollama_est["estimated_usd"] == 0.0
        assert ollama_est["free"] is True

        # Gemini cost estimate should be positive
        gemini_est = estimate_recompute_cost(conn, config, "gemini")
        assert gemini_est["job_count"] == 1
        assert gemini_est["estimated_tokens"] > 0
        assert gemini_est["estimated_usd"] >= 0.0
        assert gemini_est["free"] is False

    def test_scoring_renormalization_when_provider_none(self, embed_db: tuple[sqlite3.Connection, Config]) -> None:
        conn, config = embed_db
        none_config = replace(config, embedding=replace(config.embedding, provider="none"))

        # Weights: fit=0.50, similarity=0.30, location=0.20
        # When similarity is 0.0, fit and location should renormalize:
        # sum = 0.50 + 0.20 = 0.70
        # w_fit = 0.50 / 0.70 ≈ 0.714
        # w_loc = 0.20 / 0.70 ≈ 0.286
        conn.execute("INSERT INTO jobs (id, title, company_name, status, country, workplace_type) VALUES (20, 'Backend Eng', 'Gamma', 'open', 'IN', 'remote')")
        conn.execute(
            """INSERT INTO job_relevance (job_id, gate_status, llm_score, llm_status, rank_adjust)
               VALUES (20, 'target', 80.0, 'scored', 0.0)"""
        )
        conn.commit()

        n_scored = compute_final_scores(conn, none_config)
        assert n_scored == 1

        row = conn.execute("SELECT final_score, score_components FROM job_relevance WHERE job_id = 20").fetchone()
        assert row["final_score"] is not None
        comps = json.loads(row["score_components"])
        weights = comps["weights"]
        assert weights["percentile"] == 0.0
        # Renormalized remaining weights must sum to 1.0
        assert abs((weights["fit"] + weights["location"]) - 1.0) < 0.005
        assert weights["fit"] > 0.5
        assert weights["location"] > 0.2
        assert comps.get("similarity_disabled") is True
