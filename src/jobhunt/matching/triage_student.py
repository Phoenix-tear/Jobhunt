"""The student: rules and a small classifier mined from the judge's verdicts, so most jobs never need a model call.

Two targets, both learned from verified verdicts (my "verdict is wrong" labels weigh 3x and flip the teacher's answer):
  senior     the job is senior or asks 2+ years (level senior / lead_or_manager or required_years_min >= 2)
  not_india  the posting says a person in India cannot take it

* Rules are single tokens (a title word, a location word, a company) that are >= 90% precise on the labelled jobs with at
  least 5 supporting jobs (title and location words also need 3 different companies, otherwise they just restate one
  company; location rules use only location words and companies, never title words), and that match none of my protected jobs (applied, saved, tailored, gold-yes). Only such rules
  act, and they act like the judge would: a senior rule excludes, a location rule hides a vaguely located job.
* The classifier is numpy logistic regression on hashed word and bigram features of title, location and the evidence
  extract. It is demote-only: above the threshold that gave >= 90% precision on held-out jobs (4-fold cross-validation, every
  job is held out once) it turns a target into a maybe
  and lowers its rank; it never excludes and never promotes.
* A retrain needs 50+ new verdicts or labels and is kept only if it is not worse than the model in use.

No model call, numpy only, plain JSON on disk.
"""

from __future__ import annotations

import json
import re
import sqlite3
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from jobhunt.config import Config
from jobhunt.matching.triage import Evidence, Verdict, build_evidence
from jobhunt.matching.triage_apply import protected_ids
from jobhunt.timeutil import Clock, to_iso, utc_now

MODEL_FILE = "triage_student.json"
DIM = 4096
SEED = 20261024
TARGETS = ("senior", "not_india")
HUMAN_WEIGHT = 3.0
MIN_ROWS = 60
NEW_ROWS_NEEDED = 50
MIN_PREDICTED = 5                 # a threshold must flag at least this many held-out jobs to be trusted
MIN_COMPANIES = 3
FOLDS = 4
TOLERANCE = 0.03
_WORD = re.compile(r"[a-z0-9][a-z0-9+#.]*[a-z0-9+#]|[a-z0-9]")
_STOP = frozenset("a an and the of for to in at on with or is are be as by we you our your will this that it from".split())


# --- features -------------------------------------------------------------------------------------------------------


def tokens(text: str) -> list[str]:
    return [word for word in _WORD.findall((text or "").lower()) if word not in _STOP]


def field_tokens(evidence: Evidence) -> dict[str, list[str]]:
    """Word and bigram tokens by field (t title, l location, c company, d description extract)."""
    out: dict[str, list[str]] = {}
    for tag, text in (("t", evidence.title), ("l", evidence.location), ("c", evidence.company), ("d", evidence.extract)):
        words = tokens(text)
        out[tag] = words + [f"{a} {b}" for a, b in zip(words, words[1:])]
    return out


def feature_indexes(evidence: Evidence) -> list[int]:
    seen: set[int] = set()
    for tag, words in field_tokens(evidence).items():
        for word in words:
            seen.add(zlib.crc32(f"{tag}:{word}".encode("utf-8")) % DIM)
    return sorted(seen)


def matrix(rows: list[list[int]]) -> np.ndarray:
    data = np.zeros((len(rows), DIM), dtype=np.float32)
    for row, indexes in enumerate(rows):
        data[row, indexes] = 1.0
    return data


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -30, 30)))


def fit_logistic(features: np.ndarray, labels: np.ndarray, weights: np.ndarray, *, l2: float = 0.05, steps: int = 300,
                 rate: float = 0.5) -> tuple[np.ndarray, float]:
    """Weighted logistic regression by batch gradient descent (small data: a few hundred rows; float32 keeps it fast)."""
    x = features.astype(np.float32, copy=False)
    y = labels.astype(np.float32)
    sample = weights.astype(np.float32)
    w = np.zeros(x.shape[1], dtype=np.float32)
    b = np.float32(0.0)
    total = np.float32(float(sample.sum()) or 1.0)
    for _ in range(steps):
        error = (_sigmoid(x @ w + b).astype(np.float32) - y) * sample
        w -= np.float32(rate) * (x.T @ error / total + np.float32(l2) * w / total)
        b -= np.float32(rate) * np.float32(error.sum()) / total
    return w.astype(np.float64), float(b)


def average_precision(labels: np.ndarray, scores: np.ndarray) -> float:
    order = np.argsort(-scores)
    hits = labels[order]
    if hits.sum() == 0:
        return 0.0
    precision = np.cumsum(hits) / np.arange(1, len(hits) + 1)
    return float((precision * hits).sum() / hits.sum())


def threshold_for(labels: np.ndarray, scores: np.ndarray, target_precision: float, min_positives: int) -> dict[str, Any] | None:
    """The lowest threshold whose held-out precision is >= target (and with enough predicted positives), else None."""
    for threshold in np.round(np.arange(0.50, 1.0, 0.01), 2):
        predicted = scores >= threshold
        count = int(predicted.sum())
        if count < min_positives:
            continue
        precision = float(labels[predicted].mean())
        if precision >= target_precision:
            return {"threshold": float(threshold), "precision": round(precision, 4), "predicted": count,
                    "recall": round(float(labels[predicted].sum() / max(labels.sum(), 1)), 4)}
    return None


# --- training data --------------------------------------------------------------------------------------------------


@dataclass
class Row:
    job_id: int
    evidence: Evidence
    senior: bool
    not_india: bool
    weight: float


def build_rows(connection: sqlite3.Connection, config: Config) -> list[Row]:
    """One row per judged job: the teacher's answers, flipped where I said the verdict was wrong."""
    rows: list[Row] = []
    seen: set[int] = set()
    query = ("SELECT v.job_id, v.verified_json, v.human_label, j.title, j.company_name, j.location_raw, j.workplace_type, "
             "j.remote_regions, j.description_text FROM job_llm_verdicts v JOIN jobs j ON j.id = v.job_id "
             "WHERE v.rubric_version = ? ORDER BY v.id DESC")
    for row in connection.execute(query, (config.triage.rubric_version,)):
        if row["job_id"] in seen:
            continue
        seen.add(row["job_id"])
        verdict = Verdict.from_json(json.loads(row["verified_json"]))
        senior = (verdict.level in ("senior", "lead_or_manager")
                  or (verdict.required_years_min is not None and verdict.required_years_min >= config.triage.exclude_years_min))
        not_india = verdict.india_eligible == "no"
        label = row["human_label"]
        weight = 1.0
        if label:
            weight = HUMAN_WEIGHT
            if label in ("wrong_level", "wrong_years"):
                senior = not senior
            elif label == "wrong_location":
                not_india = not not_india
            else:
                continue                                   # "something else": direction unknown
        evidence = build_evidence(row["title"] or "", row["company_name"] or "", row["location_raw"], row["workplace_type"],
                                  row["remote_regions"], row["description_text"])
        rows.append(Row(int(row["job_id"]), evidence, senior, not_india, weight))
    return rows


def fold_of(job_id: int) -> int:
    return zlib.crc32(f"{SEED}:{job_id}".encode("utf-8")) % FOLDS


# --- rules ------------------------------------------------------------------------------------------------------------


@dataclass
class Rule:
    target: str
    field: str
    token: str
    support: int
    precision: float
    status: str = "applied"        # applied | proposed
    blocked_by: list[int] = field(default_factory=list)

    @property
    def text(self) -> str:
        names = {"t": "title", "l": "location", "c": "company", "d": "description"}
        return f"{self.target}: {names[self.field]} contains “{self.token}”"

    def matches(self, tokens_by_field: dict[str, list[str]]) -> bool:
        return self.token in set(tokens_by_field.get(self.field, ()))

    def as_json(self) -> dict[str, Any]:
        return {"target": self.target, "field": self.field, "token": self.token, "support": self.support,
                "precision": self.precision, "status": self.status, "blocked_by": self.blocked_by[:5]}


def mine_rules(rows: list[Row], protected: dict[int, Evidence], *, precision: float, min_support: int) -> list[Rule]:
    """Single-token rules with enough support and precision. A rule that matches a protected job is only proposed."""
    rules: list[Rule] = []
    token_sets = [{tag: set(words) for tag, words in field_tokens(row.evidence).items() if tag != "d"} for row in rows]
    companies = [row.evidence.company.lower() for row in rows]
    protected_tokens = {job_id: {tag: set(words) for tag, words in field_tokens(ev).items()} for job_id, ev in protected.items()}
    for target in TARGETS:
        counts: dict[tuple[str, str], list[int]] = {}
        for sets, row in zip(token_sets, rows):
            positive = 1 if getattr(row, target) else 0
            for tag, words in sets.items():
                if target == "not_india" and tag == "t":
                    continue                               # a title word says nothing about where a job is
                for word in words:
                    pair = counts.setdefault((tag, word), [0, 0])
                    pair[0] += 1
                    pair[1] += positive
        winners = [(tag, word, hits, pos) for (tag, word), (hits, pos) in counts.items()
                   if hits >= min_support and pos / hits >= precision - 1e-9
                   and (tag == "c" or len({companies[i] for i, sets in enumerate(token_sets) if word in sets.get(tag, ())}) >= MIN_COMPANIES)]
        taken: dict[frozenset[int], tuple[str, str]] = {}
        for tag, word, hits, pos in sorted(winners, key=lambda item: (-item[2], len(item[1]), item[1])):
            members = frozenset(index for index, sets in enumerate(token_sets) if word in sets.get(tag, ()))
            if members in taken:                           # same jobs already covered by a shorter rule
                continue
            taken[members] = (tag, word)
            rule = Rule(target, tag, word, hits, round(pos / hits, 4))
            rule.blocked_by = [job_id for job_id, sets in protected_tokens.items() if word in sets.get(tag, ())]
            if rule.blocked_by:
                rule.status = "proposed"
            rules.append(rule)
    return rules


# --- the model ------------------------------------------------------------------------------------------------------


@dataclass
class Opinion:
    p_senior: float | None = None
    p_not_india: float | None = None
    rules: list[Rule] = field(default_factory=list)
    senior_demote: bool = False
    india_demote: bool = False

    def as_json(self, demoted_by: str | None = None) -> dict[str, Any]:
        return {"p_senior": self.p_senior, "p_not_india": self.p_not_india, "demoted_by": demoted_by,
                "rules": [{"rule": rule.text, "target": rule.target, "precision": rule.precision, "support": rule.support}
                          for rule in self.rules]}


class Student:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.version: str = data.get("version", "")
        self.rules = [Rule(**{**r, "blocked_by": r.get("blocked_by", [])}) for r in data.get("rules", [])]
        self.models = {name: (np.asarray(m["weights"], dtype=np.float64), float(m["bias"]), m.get("threshold"))
                       for name, m in data.get("classifiers", {}).items() if m.get("threshold") is not None}

    def opinion(self, evidence: Evidence) -> Opinion:
        result = Opinion()
        by_field = field_tokens(evidence)
        result.rules = [rule for rule in self.rules if rule.status == "applied" and rule.matches(by_field)]
        indexes = feature_indexes(evidence)
        for name, (weights, bias, threshold) in self.models.items():
            probability = float(_sigmoid(np.array([weights[indexes].sum() + bias]))[0])
            if name == "senior":
                result.p_senior, result.senior_demote = round(probability, 4), probability >= threshold
            else:
                result.p_not_india, result.india_demote = round(probability, 4), probability >= threshold
        return result


def model_path(config: Config) -> Path:
    return config.resolve_path("data/models") / MODEL_FILE


def load_student(config: Config) -> Student | None:
    if not config.triage.student_enabled:
        return None
    path = model_path(config)
    if not path.is_file():
        return None
    try:
        return Student(json.loads(path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 - a corrupt file means "no student", never a crash
        return None


def student_key(student: Student | None) -> str:
    return student.version if student else ""


# --- training -------------------------------------------------------------------------------------------------------


def train(connection: sqlite3.Connection, config: Config, *, clock: Clock = utc_now) -> dict[str, Any]:
    """Fit both classifiers and mine rules. Returns the full model dict (with `trained` False and a reason when there is too
    little data)."""
    rows = build_rows(connection, config)
    now = to_iso(clock())
    out: dict[str, Any] = {"trained": False, "rows": len(rows), "at": now}
    if len(rows) < MIN_ROWS:
        out["reason"] = f"only {len(rows)} labelled jobs (need {MIN_ROWS})"
        return out
    settings = config.triage
    features = matrix([feature_indexes(row.evidence) for row in rows])
    folds = np.array([fold_of(row.job_id) for row in rows])
    classifiers: dict[str, Any] = {}
    metrics: dict[str, Any] = {}
    for target in TARGETS:
        labels = np.array([1.0 if getattr(row, target) else 0.0 for row in rows])
        weights = np.array([row.weight for row in rows])
        scores = np.zeros(len(rows))
        for fold in range(FOLDS):                          # every job is scored by a model that never saw it
            held = folds == fold
            w, b = fit_logistic(features[~held], labels[~held], weights[~held])
            scores[held] = _sigmoid(features[held] @ w + b)
        chosen = threshold_for(labels, scores, settings.student_precision, MIN_PREDICTED)
        metrics[target] = {"rows": len(rows), "positives": int(labels.sum()), "folds": FOLDS,
                           "average_precision": round(average_precision(labels, scores), 4),
                           "chosen": chosen, "trusted": chosen is not None}
        w_all, b_all = fit_logistic(features, labels, weights)         # the shipped model sees every row; the threshold is held-out
        classifiers[target] = {"weights": [round(float(x), 5) for x in w_all], "bias": round(b_all, 5),
                               "threshold": chosen["threshold"] if chosen else None}
    protected_rows = _protected_evidence(connection, config)
    rules = mine_rules(rows, protected_rows, precision=settings.student_precision, min_support=settings.student_min_support)
    out.update(trained=True, version=f"student-{now}", classifiers=classifiers, metrics=metrics,
               rules=[rule.as_json() for rule in rules], rubric_version=settings.rubric_version)
    return out


def _protected_evidence(connection: sqlite3.Connection, config: Config) -> dict[int, Evidence]:
    ids = sorted(protected_ids(connection, config))
    out: dict[int, Evidence] = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        for row in connection.execute(
                f"SELECT id, title, company_name, location_raw, workplace_type, remote_regions, description_text FROM jobs "
                f"WHERE id IN ({','.join('?' for _ in chunk)})", chunk):
            out[int(row["id"])] = build_evidence(row["title"] or "", row["company_name"] or "", row["location_raw"],
                                                 row["workplace_type"], row["remote_regions"], row["description_text"])
    return out


def _quality(model: dict[str, Any]) -> float:
    metrics = model.get("metrics", {})
    return sum((metrics.get(t, {}).get("average_precision") or 0.0) * (1.0 if metrics.get(t, {}).get("trusted") else 0.5)
               for t in TARGETS) / len(TARGETS)


def retrain_if_needed(connection: sqlite3.Connection, config: Config, *, force: bool = False,
                      clock: Clock = utc_now) -> dict[str, Any]:
    """Retrain when 50+ new verdicts or labels exist; keep the model in use if the new one is worse. Never raises on thin data."""
    path = model_path(config)
    old: dict[str, Any] = {}
    if path.is_file():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
    candidate = train(connection, config, clock=clock)
    decision: dict[str, Any] = {"at": candidate["at"], "rows": candidate["rows"]}
    if not candidate["trained"]:
        decision.update(action="skipped", reason=candidate["reason"])
        return decision
    old_rows = int(old.get("rows", 0))
    if old and not force and candidate["rows"] - old_rows < NEW_ROWS_NEEDED:
        decision.update(action="skipped", reason=f"only {candidate['rows'] - old_rows} new verdicts or labels since the last "
                                                 f"training (need {NEW_ROWS_NEEDED})")
        return decision
    new_q, old_q = _quality(candidate), _quality(old) if old else None
    decision.update(new_quality=round(new_q, 4), old_quality=None if old_q is None else round(old_q, 4))
    if old and old.get("trained") and old_q is not None and new_q < old_q - TOLERANCE:
        decision.update(action="kept_previous", reason=f"new held-out quality {new_q:.3f} is more than {TOLERANCE} below {old_q:.3f}")
        return decision
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate["last_decision"] = {**decision, "action": "retrained"}
    path.write_text(json.dumps(candidate), encoding="utf-8")
    decision.update(action="retrained", reason="enough new verdicts or labels, not worse than the previous model")
    return decision


def describe_decision(decision: dict[str, Any]) -> str:
    quality = ""
    if decision.get("new_quality") is not None:
        quality = f" (held-out quality new {decision.get('new_quality')} vs previous {decision.get('old_quality')})"
    return f"triage-student: {decision['action']}, {decision['rows']} labelled jobs, {decision.get('reason', '')}{quality}"
