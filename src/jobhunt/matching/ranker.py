"""A small local model that predicts fit from cheap features, trained on my own labels.

Used only to *rank and select* (which unscored jobs deserve a Gemini call; the order of "Best match" for
unscored jobs). It never excludes anything. Labels, strongest first: my dismiss / like clicks, the gold
file's yes/no rows, existing Gemini fit scores, and Gemini triage verdicts. A few hundred labels is small:
the cross-validated numbers are reported by `train-ranker`, and they are the honest measure of trust.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from jobhunt.config import Config
from jobhunt.decisions import LEGACY_ROLE_FIT_NEGATIVE, ROLE_FIT_NEGATIVE_REASONS
from jobhunt.matching.embeddings import effective_config
from jobhunt.matching.stats import spearman
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)

MODEL_FILE = "ranker_np.json"      # numpy-only model: plain JSON arrays, no pickle, no scikit-learn
META_FILE = "ranker.json"
SEED = 11
LEVELS = ("junior", "unspecified", "mid", "senior", "executive")
WORKPLACES = ("remote", "hybrid", "onsite", "unknown")
FAMILIES = ("ai_ml", "fullstack", "other")
SOURCE_KINDS = ("ats", "enterprise", "feed", "email", "other")
_ENTERPRISE = {"workday", "amazon_jobs", "oracle"}
_ATS = {"greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee"}


@dataclass
class Dataset:
    job_ids: list[int] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    numeric: list[list[float]] = field(default_factory=list)
    labels: list[float] = field(default_factory=list)       # 0..100
    weights: list[float] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)          # which label source produced the row

    def __len__(self) -> int:
        return len(self.job_ids)


def source_kind(sources: str | None) -> str:
    names = set((sources or "").split(","))
    if names & _ENTERPRISE:
        return "enterprise"
    if names & _ATS:
        return "ats"
    if any(name.startswith("email_") for name in names):
        return "email"
    return "feed" if names - {""} else "other"


def _one_hot(value: str | None, options: tuple[str, ...]) -> list[float]:
    return [1.0 if value == option else 0.0 for option in options]


_SELECT = """
SELECT j.id, j.title, j.country, j.workplace_type, j.remote_eligible_india, j.work_auth_required,
       r.similarity_ai, r.similarity_fullstack, r.similarity_percentile, r.seniority_level, r.required_years,
       r.role_family, r.rank_adjust, r.llm_score, r.llm_status, r.llm_flags, r.local_llm_score, r.gate_status,
       (SELECT GROUP_CONCAT(DISTINCT s.source) FROM job_sightings s WHERE s.job_id = j.id) AS sources
FROM jobs j JOIN job_relevance r ON r.job_id = j.id
WHERE r.similarity_ai IS NOT NULL
"""


def features(row: sqlite3.Row) -> list[float]:
    sim_ai = float(row["similarity_ai"] or 0.0)
    sim_full = float(row["similarity_fullstack"] or 0.0)
    years = row["required_years"]
    eligible = row["remote_eligible_india"]
    local = row["local_llm_score"]
    return [
        sim_ai, sim_full, max(sim_ai, sim_full), sim_ai - sim_full, float(row["similarity_percentile"] or 0.0),
        float(row["rank_adjust"] or 0.0),
        float(years) if years is not None else 0.0, 1.0 if years is not None else 0.0,
        1.0 if row["country"] == "IN" else 0.0,
        1.0 if eligible == 1 else 0.0, 1.0 if eligible == 0 else 0.0,
        1.0 if row["work_auth_required"] else 0.0,
        float(local) / 100.0 if local is not None else 0.0, 1.0 if local is not None else 0.0,
        *_one_hot(row["seniority_level"], LEVELS), *_one_hot(row["workplace_type"], WORKPLACES),
        *_one_hot(row["role_family"], FAMILIES), *_one_hot(source_kind(row["sources"]), SOURCE_KINDS),
    ]


FEATURE_COUNT = len(features({  # type: ignore[arg-type]
    "similarity_ai": 0, "similarity_fullstack": 0, "similarity_percentile": 0, "rank_adjust": 0, "required_years": None,
    "country": None, "remote_eligible_india": None, "work_auth_required": 0, "local_llm_score": None,
    "seniority_level": None, "workplace_type": None, "role_family": None, "sources": None,
}))


GOLD_FILE = "data/eval/gold.csv"
GOLD_FALLBACK_FILE = "data/eval/gold_labeled.csv"
_YES = {"yes", "y", "1", "true"}
_NO = {"no", "n", "0", "false"}


def resolve_gold_path(config: Config, override: Path | None = None) -> Path:
    """`gold.csv`, or `gold_labeled.csv` (one warning line) when only that exists."""
    if override is not None:
        return override
    primary = config.resolve_path(GOLD_FILE)
    if primary.is_file():
        return primary
    fallback = config.resolve_path(GOLD_FALLBACK_FILE)
    if fallback.is_file():
        print(f"warning: {GOLD_FILE} not found; using {GOLD_FALLBACK_FILE}")
        return fallback
    return primary


def _gold_labels(path: Path) -> dict[int, float]:
    if not path.is_file():
        return {}
    labels: dict[int, float] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            answer = (row.get("relevant") or "").strip().lower()
            try:
                job_id = int(row["job_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if answer in _YES:
                labels[job_id] = 90.0
            elif answer in _NO:
                labels[job_id] = 10.0
    return labels


def build_dataset(connection: sqlite3.Connection, config: Config) -> Dataset:
    """One row per labelled job; the strongest label source wins for a job."""
    gold = _gold_labels(resolve_gold_path(config))
    feedback = {int(r["job_id"]): (r["action"], r["reason"]) for r in connection.execute(
        "SELECT job_id, action, reason FROM job_feedback_current")}
    decided = {int(r["job_id"]): (r["action"], r["reason"]) for r in connection.execute(
        "SELECT job_id, action, reason FROM job_decision_current")}
    for r in connection.execute("SELECT job_id, stage FROM applications"):      # tracker moves count like decisions
        decided.setdefault(int(r["job_id"]), ("saved" if r["stage"] == "saved" else "applied", None))
    data = Dataset()
    for row in connection.execute(_SELECT):
        job_id = int(row["id"])
        try:
            flags = json.loads(row["llm_flags"]) if row["llm_flags"] else {}
        except ValueError:
            flags = {}
        if isinstance(flags, dict) and flags.get("injection_suspected"):
            continue                                            # never learn from injection-flagged text
        label: float | None = None
        weight, kind = 1.0, ""
        # My decisions come first. Only role-fit statements teach role fit: "applied" is a strong positive, "saved" a
        # positive, wrong_role / too_senior / wrong_domain_or_stack negatives. Ineligible, location, company, suspicious
        # posting, already-applied and "other" say nothing about whether the ROLE fits, so they are not labels.
        decision = decided.get(job_id)
        legacy = feedback.get(job_id)
        if decision and decision[0] == "applied":
            label, weight, kind = 100.0, 3.0, "applied"
        elif decision and decision[0] == "saved":
            label, weight, kind = 80.0, 2.0, "saved"
        elif decision and decision[0] == "not_applying" and decision[1] in ROLE_FIT_NEGATIVE_REASONS:
            label, weight, kind = 0.0, 3.0, "not_applying_role"
        elif decision is None and legacy and legacy[0] == "like":
            label, weight, kind = 100.0, 3.0, "feedback"
        elif decision is None and legacy and legacy[0] == "dismiss" and legacy[1] in LEGACY_ROLE_FIT_NEGATIVE:
            label, weight, kind = 0.0, 3.0, "feedback"
        elif job_id in gold:
            label, weight, kind = gold[job_id], 3.0, "gold"
        elif row["llm_status"] == "scored" and row["llm_score"] is not None:
            label, weight, kind = float(row["llm_score"]), 1.0, "gemini_score"
        elif isinstance(flags, dict) and isinstance(flags.get("classification"), dict):
            relevant = flags["classification"].get("relevant_role")
            if relevant is not None:
                label, weight, kind = (60.0 if relevant else 10.0), 0.5, "gemini_classify"
        if label is None:
            continue
        data.job_ids.append(job_id)
        data.titles.append(row["title"] or "")
        data.numeric.append(features(row))
        data.labels.append(label)
        data.weights.append(weight)
        data.kinds.append(kind)
    return data


_TOKEN = re.compile(r"(?u)\b\w\w+\b")


def _terms(title: str) -> list[str]:
    words = _TOKEN.findall(title.lower())
    return words + [f"{left} {right}" for left, right in zip(words, words[1:])]


def fit_vocabulary(titles: list[str], *, min_df: int = 2, max_features: int = 300) -> dict[str, Any]:
    """Title uni/bi-gram vocabulary with smoothed idf (a sublinear TF-IDF recipe, numpy only)."""
    document_frequency: dict[str, int] = {}
    for title in titles:
        for term in set(_terms(title)):
            document_frequency[term] = document_frequency.get(term, 0) + 1
    kept = [(term, count) for term, count in document_frequency.items() if count >= min_df]
    kept.sort(key=lambda item: (-item[1], item[0]))
    kept = kept[:max_features]
    total = len(titles)
    return {"terms": [term for term, _ in kept],
            "idf": [float(np.log((1 + total) / (1 + df)) + 1.0) for _, df in kept]}


def _title_matrix(vocabulary: dict[str, Any], titles: list[str]) -> np.ndarray:
    terms = vocabulary["terms"]
    index = {term: i for i, term in enumerate(terms)}
    matrix = np.zeros((len(titles), len(terms)), dtype="float64")
    for row, title in enumerate(titles):
        for term in _terms(title):
            column = index.get(term)
            if column is not None:
                matrix[row, column] += 1.0
    present = matrix > 0
    matrix = np.where(present, 1.0 + np.log(np.where(present, matrix, 1.0)), 0.0)
    matrix = matrix * np.asarray(vocabulary["idf"], dtype="float64")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms > 0, norms, 1.0)


@dataclass
class RidgeModel:
    """Weighted ridge on standardised numeric features plus optional title TF-IDF columns."""

    alpha: float
    use_titles: bool
    mean: np.ndarray | None = None
    scale: np.ndarray | None = None
    coef: np.ndarray | None = None
    intercept: float = 0.0
    vocabulary: dict[str, Any] | None = None

    def _design(self, titles: list[str], numeric: np.ndarray) -> np.ndarray:
        scaled = (numeric - self.mean) / self.scale
        if self.use_titles and self.vocabulary and self.vocabulary["terms"]:
            return np.hstack([scaled, _title_matrix(self.vocabulary, titles)])
        return scaled

    def fit(self, titles: list[str], numeric: np.ndarray, labels: np.ndarray, weights: np.ndarray) -> "RidgeModel":
        self.mean = numeric.mean(axis=0)
        spread = numeric.std(axis=0)
        self.scale = np.where(spread > 1e-9, spread, 1.0)
        self.vocabulary = fit_vocabulary(titles) if self.use_titles else None
        design = self._design(titles, numeric)
        total = float(weights.sum())
        design_mean = (weights[:, None] * design).sum(axis=0) / total
        label_mean = float((weights * labels).sum() / total)
        centred = design - design_mean
        weighted = centred * weights[:, None]
        gram = centred.T @ weighted + self.alpha * np.eye(design.shape[1])
        self.coef = np.linalg.solve(gram, weighted.T @ (labels - label_mean))
        self.intercept = label_mean - float(design_mean @ self.coef)
        return self

    def predict(self, titles: list[str], numeric: np.ndarray) -> np.ndarray:
        return self._design(titles, numeric) @ self.coef + self.intercept

    def to_dict(self) -> dict[str, Any]:
        return {"alpha": self.alpha, "use_titles": self.use_titles, "mean": self.mean.tolist(),
                "scale": self.scale.tolist(), "coef": self.coef.tolist(), "intercept": self.intercept,
                "vocabulary": self.vocabulary}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RidgeModel":
        return cls(alpha=float(data["alpha"]), use_titles=bool(data["use_titles"]),
                   mean=np.asarray(data["mean"], dtype="float64"), scale=np.asarray(data["scale"], dtype="float64"),
                   coef=np.asarray(data["coef"], dtype="float64"), intercept=float(data["intercept"]),
                   vocabulary=data.get("vocabulary"))


def _candidates() -> dict[str, RidgeModel]:
    return {
        "ridge": RidgeModel(alpha=3.0, use_titles=True),
        "ridge_numeric": RidgeModel(alpha=3.0, use_titles=False),
    }


def _fold_indices(count: int, folds: int, seed: int) -> list[np.ndarray]:
    order = np.random.RandomState(seed).permutation(count)
    return [chunk for chunk in np.array_split(order, folds) if len(chunk)]


@dataclass
class TrainReport:
    trained: bool
    reason: str = ""
    rows: int = 0
    label_sources: dict[str, int] = field(default_factory=dict)
    chosen: str = ""
    cv: dict[str, dict[str, float | None]] = field(default_factory=dict)
    path: str = ""
    positives: int = 0          # rows whose label is "relevant" (>= 60) from any source
    gold_rows: int = 0
    version: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def train(connection: sqlite3.Connection, config: Config, *, folds: int = 5, seed: int = SEED,
          clock: Clock = utc_now, min_rows: int = 40, write: bool = True) -> TrainReport:
    data = build_dataset(connection, config)
    sources: dict[str, int] = {}
    for kind in data.kinds:
        sources[kind] = sources.get(kind, 0) + 1
    positives = sum(1 for label in data.labels if label >= 60.0)
    if len(data) < min_rows:
        return TrainReport(False, f"only {len(data)} labelled jobs (need {min_rows})", len(data), sources,
                           positives=positives, gold_rows=sources.get("gold", 0))

    labels = np.asarray(data.labels, dtype="float64")
    weights = np.asarray(data.weights, dtype="float64")
    titles = list(data.titles)
    numeric = np.asarray(data.numeric, dtype="float64")
    chunks = _fold_indices(len(data), min(folds, len(data) // 8 or 2), seed)

    cv: dict[str, dict[str, float | None]] = {}
    for name in _candidates():
        predictions = np.zeros(len(data))
        for test_index in chunks:
            train_index = np.setdiff1d(np.arange(len(data)), test_index)
            model = _candidates()[name].fit([titles[i] for i in train_index], numeric[train_index],
                                            labels[train_index], weights[train_index])
            predictions[test_index] = model.predict([titles[i] for i in test_index], numeric[test_index])
        rho = spearman(predictions.tolist(), labels.tolist())
        mae = float(np.mean(np.abs(predictions - labels)))
        top = max(1, len(data) // 5)
        overlap = len(set(np.argsort(-predictions)[:top]) & set(np.argsort(-labels)[:top])) / top
        cv[name] = {"spearman": None if rho is None else round(rho, 3), "mae": round(mae, 2),
                    f"top{top}_overlap": round(overlap, 3)}

    chosen = max(cv, key=lambda name: -2.0 if cv[name]["spearman"] is None else float(cv[name]["spearman"]))
    # the yardstick the ranker has to beat: raw best similarity (feature column 2) against the same labels
    baseline = spearman(numeric[:, 2].tolist(), labels.tolist())
    cv["similarity_only"] = {"spearman": None if baseline is None else round(baseline, 3)}
    model = _candidates()[chosen].fit(titles, numeric, labels, weights)

    directory = config.resolve_path("data/models")
    version = f"{chosen}-{len(data)}rows-{clock().strftime('%Y%m%d')}"
    meta = {"version": version, "rows": len(data), "label_sources": sources, "chosen": chosen, "cv": cv,
            "trained_at": to_iso(clock()), "seed": seed, "features": FEATURE_COUNT, "positives": positives,
            "engine": "numpy-ridge", "human_labels": sum(sources.get(k, 0) for k in HUMAN_KINDS), "in_use": "new"}
    if write:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / MODEL_FILE).write_text(json.dumps({"version": version, "model": model.to_dict()}), encoding="utf-8")
        (directory / META_FILE).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    report = TrainReport(True, "", len(data), sources, chosen, cv, str(directory / MODEL_FILE),
                         positives=positives, gold_rows=sources.get("gold", 0), version=version, meta=meta)
    report.model = model                                         # type: ignore[attr-defined]
    return report


HUMAN_KINDS = ("applied", "saved", "not_applying_role", "feedback", "gold")      # labels that come from me, not Gemini


def read_meta(config: Config) -> dict[str, Any]:
    path = config.resolve_path("data/models") / META_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


def _chosen_spearman(meta: dict[str, Any]) -> float | None:
    value = (meta.get("cv", {}).get(meta.get("chosen", ""), {}) or {}).get("spearman")
    return None if value is None else float(value)


def retrain_if_needed(connection: sqlite3.Connection, config: Config, *, new_labels_needed: int = 10,
                      min_usable: int = 100, tolerance: float = 0.03, clock: Clock = utc_now) -> dict[str, Any]:
    """Retrain at the end of `match` when 10+ new labels exist; keep the previous model if the new one is worse.

    Numpy only, no model call. Guards: fewer than `min_usable` labels keeps the previous model; a cross-validated
    Spearman more than `tolerance` below the previous one keeps it too. The decision and numbers are stored in
    the meta file (shown on Health) and returned (printed into the scheduled-run detail)."""
    old = read_meta(config)
    candidate = train(connection, config, clock=clock, write=False)
    decision: dict[str, Any] = {"at": to_iso(clock()), "rows": candidate.rows, "labels_by_type": candidate.label_sources}
    old_rows = int(old.get("rows", 0))
    if not candidate.trained:
        decision.update(action="skipped", reason=candidate.reason)
    elif old and candidate.rows - old_rows < new_labels_needed:
        decision.update(action="skipped", reason=f"only {candidate.rows - old_rows} new labels since the last training "
                                                 f"(need {new_labels_needed})")
    elif old and candidate.rows < min_usable:
        decision.update(action="kept_previous", reason=f"only {candidate.rows} usable labels (need {min_usable})")
    else:
        new_rho, old_rho = _chosen_spearman(candidate.meta), _chosen_spearman(old)
        decision.update(new_spearman=new_rho, old_spearman=old_rho)
        if old and old_rho is not None and (new_rho is None or new_rho < old_rho - tolerance):
            decision.update(action="kept_previous", reason=f"new CV Spearman {new_rho} is more than {tolerance} below {old_rho}")
        else:
            directory = config.resolve_path("data/models")
            directory.mkdir(parents=True, exist_ok=True)
            (directory / MODEL_FILE).write_text(
                json.dumps({"version": candidate.version, "model": candidate.model.to_dict()}),  # type: ignore[attr-defined]
                encoding="utf-8")
            candidate.meta["last_decision"] = {**decision, "action": "retrained", "reason": "enough new labels, not worse"}
            (directory / META_FILE).write_text(json.dumps(candidate.meta, indent=2), encoding="utf-8")
            decision.update(action="retrained", reason="enough new labels, not worse than the previous model")
            return decision
    if old:                                   # keep the model files; only record what was decided
        old["last_decision"] = decision
        old["in_use"] = "previous" if decision["action"] == "kept_previous" else old.get("in_use", "new")
        old["labels_by_type"] = candidate.label_sources
        (config.resolve_path("data/models") / META_FILE).write_text(json.dumps(old, indent=2), encoding="utf-8")
    return decision


def describe_decision(decision: dict[str, Any]) -> str:
    numbers = ""
    if decision.get("new_spearman") is not None or decision.get("old_spearman") is not None:
        numbers = f" (CV Spearman new {decision.get('new_spearman')} vs previous {decision.get('old_spearman')})"
    return f"ranker-retrain: {decision['action']}, {decision['rows']} labels, {decision.get('reason', '')}{numbers}"


def load_model(config: Config) -> dict[str, Any] | None:
    path = config.resolve_path("data/models") / MODEL_FILE
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {"version": raw["version"], "model": RidgeModel.from_dict(raw["model"])}
    except Exception:  # noqa: BLE001 - a corrupt file means "no ranker", never a crash
        logger.warning("could not load %s", path)
        return None


def predict_all(connection: sqlite3.Connection, config: Config, *, clock: Clock = utc_now) -> int:
    """Store the ranker's predicted fit for every open target/maybe job with similarities."""
    bundle = load_model(config)
    if bundle is None:
        return 0
    rows = connection.execute(
        _SELECT + " AND j.status = 'open' AND r.gate_status IN ('target', 'maybe')").fetchall()
    if not rows:
        return 0
    titles = [row["title"] or "" for row in rows]
    numeric = [features(row) for row in rows]
    predicted = np.clip(bundle["model"].predict(titles, np.asarray(numeric, dtype="float64")), 0.0, 100.0)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.executemany(
            "UPDATE job_relevance SET local_rank_score = ?, local_rank_version = ? WHERE job_id = ?",
            [(round(float(score), 2), bundle["version"], int(row["id"])) for row, score in zip(rows, predicted)],
        )
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
    return len(rows)


__all__ = ["Dataset", "TrainReport", "build_dataset", "effective_config", "features", "load_model", "predict_all",
           "resolve_gold_path", "retrain_if_needed", "describe_decision", "read_meta", "source_kind", "train"]
