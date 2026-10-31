"""Local ranker: dataset building, deterministic training, prediction storage, candidate selection."""

from __future__ import annotations

import dataclasses
import json
import sqlite3

import pytest

from jobhunt.config import Config
from jobhunt.matching import ranker
from tests.test_scoring import add_job


@pytest.fixture()
def rcfg(config: Config, tmp_path) -> Config:  # noqa: ANN001
    """The real config, with the project root moved so models and gold files land in tmp_path."""
    return dataclasses.replace(config, project_root=tmp_path)


def seed_labelled(connection: sqlite3.Connection, count: int = 60) -> list[int]:
    ids = []
    for n in range(count):
        good = n % 2 == 0
        title = f"{'Machine Learning' if good else 'Support'} Engineer {n}"
        job_id = add_job(connection, n, title, "Python models.", country="IN" if good else "US")
        sim = 0.72 + (n % 7) * 0.004 if good else 0.50 + (n % 7) * 0.004
        flags = {"scored": {"matched_skills": []}}
        if n % 11 == 0:
            flags["injection_suspected"] = True
        connection.execute(
            """INSERT INTO job_relevance (job_id, computed_at, rules_hash, input_hash, gate_status, gate_rule,
                   gate_reason, role_family, similarity_ai, similarity_fullstack, similarity_percentile, llm_score,
                   llm_status, llm_flags, seniority_level)
               VALUES (?, 'x', 'r', 'i', 'target', 'title_ai_ml', 'ok', ?, ?, ?, ?, ?, 'scored', ?, 'unspecified')""",
            (job_id, "ai_ml" if good else "other", sim, sim - 0.05, 0.9 if good else 0.2,
             85.0 + (n % 5) if good else 20.0 + (n % 5), json.dumps(flags)),
        )
        ids.append(job_id)
    return ids


def test_dataset_uses_the_strongest_label_and_skips_injection_text(connection: sqlite3.Connection, rcfg: Config) -> None:
    ids = seed_labelled(connection, 12)
    connection.execute("INSERT INTO job_feedback (job_id, action, reason, created_at) VALUES (?, 'dismiss', 'wrong_role', 'x')",
                       (ids[1],))
    connection.execute("INSERT INTO job_feedback (job_id, action, reason, created_at) VALUES (?, 'dismiss', 'other', 'x')",
                       (ids[5],))                       # a non-role reason is not a role-fit label
    connection.execute("INSERT INTO job_feedback (job_id, action, created_at) VALUES (?, 'like', 'x')", (ids[3],))
    data = ranker.build_dataset(connection, rcfg)
    by_id = {job_id: (label, kind) for job_id, label, kind in zip(data.job_ids, data.labels, data.kinds)}
    assert ids[0] not in by_id                                   # n=0: injection-flagged, skipped
    assert by_id[ids[1]] == (0.0, "feedback") and by_id[ids[3]] == (100.0, "feedback")
    assert by_id[ids[2]][1] == "gemini_score"
    assert by_id[ids[5]][1] == "gemini_score"                    # dismissed as 'other': falls back to the Gemini score


def test_gold_labels_outrank_gemini_scores(connection: sqlite3.Connection, rcfg: Config) -> None:
    ids = seed_labelled(connection, 6)
    gold = rcfg.resolve_path("data/eval/gold.csv")
    gold.parent.mkdir(parents=True)
    gold.write_text(f"job_id,title,relevant\n{ids[2]},x,no\n{ids[3]},y,yes\n{ids[4]},z,\n", encoding="utf-8")
    data = ranker.build_dataset(connection, rcfg)
    by_id = {job_id: (label, kind) for job_id, label, kind in zip(data.job_ids, data.labels, data.kinds)}
    assert by_id[ids[2]] == (10.0, "gold") and by_id[ids[3]] == (90.0, "gold")
    assert by_id[ids[4]][1] == "gemini_score"                    # blank answer: ignored


def test_too_few_labels_does_not_train(connection: sqlite3.Connection, rcfg: Config) -> None:
    seed_labelled(connection, 10)
    report = ranker.train(connection, rcfg)
    assert report.trained is False and "labelled" in report.reason


def test_training_is_deterministic_and_predictions_follow_the_labels(connection: sqlite3.Connection, rcfg: Config) -> None:
    ids = seed_labelled(connection, 80)
    first = ranker.train(connection, rcfg, seed=3)
    second = ranker.train(connection, rcfg, seed=3)
    assert first.trained and first.chosen in {"ridge", "ridge_numeric"}
    assert first.cv == second.cv                                  # same seed, same cross-validation numbers
    assert max(v["spearman"] for v in first.cv.values()) > 0.6
    stored = ranker.predict_all(connection, rcfg)
    assert stored == 80
    scores = {r["job_id"]: r["local_rank_score"] for r in connection.execute(
        "SELECT job_id, local_rank_score FROM job_relevance")}
    assert scores[ids[2]] > scores[ids[1]]                        # the "good" kind outranks the "bad" kind
    assert (rcfg.resolve_path("data/models") / ranker.META_FILE).is_file()


def test_prediction_without_a_model_is_a_noop(connection: sqlite3.Connection, rcfg: Config) -> None:
    seed_labelled(connection, 4)
    assert ranker.predict_all(connection, rcfg) == 0


def test_source_kind_buckets() -> None:
    assert ranker.source_kind("workday") == "enterprise"
    assert ranker.source_kind("greenhouse,email_linkedin") == "ats"
    assert ranker.source_kind("email_linkedin") == "email"
    assert ranker.source_kind("remotive") == "feed"
    assert ranker.source_kind(None) == "other"


def test_gold_falls_back_to_gold_labeled_and_accepts_label_spellings(
        connection: sqlite3.Connection, rcfg: Config, capsys) -> None:
    ids = seed_labelled(connection, 10)[1:]          # index 0 is injection-flagged and never used
    directory = rcfg.resolve_path("data/eval")
    directory.mkdir(parents=True)
    rows = ["YES", "No", "TRUE", "0", "1", "False", "maybe"]
    body = "\n".join(["job_id,title,relevant"] + [f"{ids[i]},t,{value}" for i, value in enumerate(rows)]) + "\n"
    (directory / "gold_labeled.csv").write_text(body, encoding="utf-8")
    data = ranker.build_dataset(connection, rcfg)
    gold = {job_id: label for job_id, label, kind in zip(data.job_ids, data.labels, data.kinds) if kind == "gold"}
    assert gold == {ids[0]: 90.0, ids[1]: 10.0, ids[2]: 90.0, ids[3]: 10.0, ids[4]: 90.0, ids[5]: 10.0}
    assert "gold_labeled.csv" in capsys.readouterr().out
    (directory / "gold.csv").write_text("job_id,title,relevant\n", encoding="utf-8")
    assert ranker.resolve_gold_path(rcfg) == directory / "gold.csv"           # the primary name wins when present
