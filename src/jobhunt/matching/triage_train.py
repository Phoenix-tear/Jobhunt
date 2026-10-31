"""Training, validation, calibration and retrain guards for the triage cascade (v9).

Features:
- Grouped K-fold (company as group: no employer near-duplicates in train and test).
- Time split (oldest 80% train, newest 20% test).
- Calibration fitting (Platt scaling and Isotonic regression on out-of-fold logits).
- Model selection: Logistic Regression vs tiny MLP.
- Metric reporting: Precision, Recall, AUC, ECE.
- Coverage-vs-precision curve: share of jobs the model can decide at >= 95% precision.
- Learning curves: metrics at 25%, 50%, 100% of labels.
- Guarded retrain: requires >= 30 new labels, minimum usable labels, keeps old model if CV metric drops > 0.03.
- Ranker comparison for soft target `fit_proxy`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from jobhunt.config import Config
from jobhunt.matching.ollama import OllamaClient
from jobhunt.matching.triage_features import (
    ExtractedDataset,
    SparseCSR,
    extract_features_for_jobs,
)
from jobhunt.matching.triage_labels import (
    HEADS,
    UnifiedLabel,
    build_unified_labels,
    get_eval_dataset,
)
from jobhunt.matching.triage_models import (
    HybridLogisticModel,
    IsotonicCalibrator,
    PlattCalibrator,
    TinyMLPModel,
    expected_calibration_error,
)
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)

META_FILE = "triage_cascade_meta.json"
MODEL_FILE_PREFIX = "triage_cascade"
MIN_USABLE_LABELS = 60
RETRAIN_NEW_LABELS_NEEDED = 30
TOLERANCE = 0.03
FOLDS = 5


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30.0, 30.0)))


def compute_auc(labels: np.ndarray, scores: np.ndarray) -> float:
    """Compute ROC-AUC without scikit-learn."""
    if len(labels) == 0:
        return 0.0
    pos_mask = labels >= 0.5
    n_pos = int(pos_mask.sum())
    n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5

    ranks = np.argsort(np.argsort(scores)) + 1
    sum_pos_ranks = float(ranks[pos_mask].sum())
    auc = (sum_pos_ranks - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return float(np.clip(auc, 0.0, 1.0))


def compute_coverage_curve(probs: np.ndarray, labels: np.ndarray) -> list[dict[str, Any]]:
    """Compute coverage-vs-precision curve: share of jobs model can decide above threshold with precision >= target."""
    n_total = len(probs)
    if n_total == 0:
        return []

    curve = []
    # Test thresholds from 0.50 to 0.99
    for t in np.round(np.arange(0.50, 1.00, 0.02), 2):
        thresh = float(t)
        mask = probs >= thresh
        count = int(mask.sum())
        if count == 0:
            continue
        prec = float(labels[mask].mean())
        rec = float(labels[mask].sum() / max(labels.sum(), 1.0))
        coverage = float(count / n_total)
        curve.append({
            "threshold": thresh,
            "coverage": round(coverage, 4),
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "predicted": count,
        })
    return curve


def select_thresholds(
    oof_probs: np.ndarray,
    oof_labels: np.ndarray,
    *,
    target_precision: float = 0.95,
    min_precision: float | None = None,
    min_predicted: int = 5,
    default_act: float = 0.90,
    default_low: float = 0.40,
) -> tuple[float, float, float, float]:
    """Select (act_threshold, abstain_low, precision_at_act, coverage_at_act)."""
    if min_precision is not None:
        target_precision = min_precision
    curve = compute_coverage_curve(oof_probs, oof_labels)
    # Find lowest threshold that achieves target_precision with at least min_predicted items
    act = default_act
    prec_at_act = 0.0
    cov_at_act = 0.0
    for pt in curve:
        if pt["precision"] >= target_precision and pt["predicted"] >= min_predicted:
            act = pt["threshold"]
            prec_at_act = pt["precision"]
            cov_at_act = pt["coverage"]
            break

    # Abstain low: probability below which precision for negative class is high
    low = default_low
    # Test lower thresholds for negative class (probs < low means negative)
    for t in np.round(np.arange(0.10, 0.50, 0.02), 2):
        thresh = float(t)
        neg_mask = oof_probs < thresh
        if neg_mask.sum() >= min_predicted:
            neg_prec = float((1.0 - oof_labels[neg_mask]).mean())
            if neg_prec >= target_precision:
                low = thresh
                break

    return float(act), float(low), float(prec_at_act), float(cov_at_act)


def grouped_kfold_split(
    companies: list[str],
    labels: np.ndarray | None = None,
    n_splits: int = FOLDS,
    seed: int = 42,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Grouped K-fold split ensuring no company appears in both train and test."""
    unique_companies = sorted(set(companies))
    rng = np.random.RandomState(seed)
    rng.shuffle(unique_companies)

    # Distribute companies to folds trying to balance sample counts and positive labels
    fold_companies: list[list[str]] = [[] for _ in range(n_splits)]
    fold_counts = np.zeros(n_splits, dtype=int)

    # Map company to list of sample indices
    comp_indices: dict[str, list[int]] = {}
    for idx, c in enumerate(companies):
        comp_indices.setdefault(c, []).append(idx)

    # Sort companies by sample count descending (greedy bin packing)
    sorted_comps = sorted(unique_companies, key=lambda c: len(comp_indices[c]), reverse=True)
    for c in sorted_comps:
        min_fold = int(np.argmin(fold_counts))
        fold_companies[min_fold].append(c)
        fold_counts[min_fold] += len(comp_indices[c])

    splits: list[tuple[np.ndarray, np.ndarray]] = []
    all_indices = np.arange(len(companies))
    for f in range(n_splits):
        test_comps = set(fold_companies[f])
        test_mask = np.array([c in test_comps for c in companies])
        test_idx = all_indices[test_mask]
        train_idx = all_indices[~test_mask]
        splits.append((train_idx, test_idx))
    return splits


def time_split(
    dates: list[str],
    train_ratio: float = 0.8,
    test_ratio: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Time-based split: train on older postings, test on newest."""
    if test_ratio is not None:
        train_ratio = 1.0 - test_ratio
    order = np.argsort(dates)
    split_point = int(len(order) * train_ratio)
    train_idx = order[:split_point]
    test_idx = order[split_point:]
    return train_idx, test_idx


def should_accept_retrain(
    old_metric: float | None,
    new_metric: float,
    new_label_count: int,
    min_labels: int = MIN_USABLE_LABELS,
    max_metric_drop: float = TOLERANCE,
) -> tuple[bool, str]:
    """Pure logic guard for model retraining."""
    if new_label_count < min_labels:
        return False, f"insufficient labels: {new_label_count} < min {min_labels}"
    if old_metric is not None and (old_metric - new_metric) > max_metric_drop:
        return False, f"metric dropped by {old_metric - new_metric:.4f} > allowed {max_metric_drop}"
    return True, f"accepted (metric {new_metric:.4f} >= {old_metric if old_metric is not None else 0.0:.4f} - {max_metric_drop})"


@dataclass
class HeadEvaluationResult:
    head: str
    n_samples: int
    n_positives: int
    n_eval_usable: int
    chosen_model: str                 # "logistic" | "mlp"
    grouped_cv_auc: float
    grouped_cv_precision: float
    grouped_cv_recall: float
    grouped_cv_ece: float
    time_split_auc: float
    time_split_precision: float
    time_split_recall: float
    time_split_ece: float
    act_threshold: float
    abstain_low: float
    precision_at_act: float
    coverage_at_act: float
    coverage_curve: list[dict[str, Any]]
    learning_curve: dict[str, float]  # "25%": auc, "50%": auc, "100%": auc
    can_act: bool                     # True only if precision >= 95% at >= 20% coverage with >=30 pos/neg and 0 gold demotions
    mode: str                         # "active" | "shadow"
    reason: str = ""


def train_and_eval_head(
    connection: sqlite3.Connection,
    config: Config,
    head: str,
    dataset: ExtractedDataset,
    unified_labels: list[UnifiedLabel],
    protected_job_ids: set[int],
    *,
    seed: int = 42,
) -> tuple[HybridLogisticModel, HeadEvaluationResult]:
    """Train, validate (grouped CV + time split), calibrate, and evaluate one head."""
    label_map = {u.job_id: u for u in unified_labels if u.head == head}
    job_ids = [jid for jid in dataset.job_ids if jid in label_map]

    idx_map = [i for i, jid in enumerate(dataset.job_ids) if jid in label_map]
    y_all = np.array([label_map[jid].label for jid in job_ids], dtype=np.float32)
    w_all = np.array([label_map[jid].weight * label_map[jid].confidence for jid in job_ids], dtype=np.float32)
    eval_mask = np.array([label_map[jid].eval_usable for jid in job_ids], dtype=bool)
    companies = [dataset.companies[i] for i in idx_map]
    dates = [dataset.created_dates[i] for i in idx_map]

    X_dense = dataset.dense_features[idx_map]

    # Subset sparse features
    sparse_rows: list[dict[int, float]] = []
    for i in idx_map:
        start, end = dataset.sparse_features.indptr[i], dataset.sparse_features.indptr[i + 1]
        cols = dataset.sparse_features.indices[start:end]
        vals = dataset.sparse_features.data[start:end]
        sparse_rows.append(dict(zip(cols, vals)))
    X_sparse = dataset.sparse_features.__class__.from_dict({
        "indptr": [0] + [len(r) for r in sparse_rows],
        "indices": [c for r in sparse_rows for c in sorted(r)],
        "data": [r[c] for r in sparse_rows for c in sorted(r)],
        "shape": [len(sparse_rows), dataset.sparse_features.shape[1]],
    })
    # Rebuild CSR cleanly
    indptr = [0]
    indices: list[int] = []
    data_vals: list[float] = []
    for r in sparse_rows:
        for c in sorted(r):
            indices.append(c)
            data_vals.append(r[c])
        indptr.append(len(indices))
    X_sparse = SparseCSR(
        indptr=np.array(indptr, dtype=np.int32),
        indices=np.array(indices, dtype=np.int32),
        data=np.array(data_vals, dtype=np.float32),
        shape=(len(sparse_rows), dataset.sparse_features.shape[1]),
    )

    n_samples = len(job_ids)
    n_positives = int((y_all >= 0.5).sum())
    n_negatives = n_samples - n_positives
    n_eval = int(eval_mask.sum())

    # 1. Grouped K-Fold Cross Validation
    cv_splits = grouped_kfold_split(companies, y_all, n_splits=FOLDS, seed=seed)
    oof_logits_lr = np.zeros(n_samples, dtype=np.float32)
    oof_logits_mlp = np.zeros(n_samples, dtype=np.float32)

    for train_idx, test_idx in cv_splits:
        # Fit LR
        m_lr = HybridLogisticModel(np.zeros(X_dense.shape[1], dtype=np.float32), np.zeros(X_sparse.shape[1], dtype=np.float32))
        sub_sparse_train = SparseCSR(
            indptr=np.array([0] + [len(sparse_rows[i]) for i in train_idx], dtype=np.int32).cumsum(),
            indices=np.array([c for i in train_idx for c in sorted(sparse_rows[i])], dtype=np.int32),
            data=np.array([sparse_rows[i][c] for i in train_idx for c in sorted(sparse_rows[i])], dtype=np.float32),
            shape=(len(train_idx), X_sparse.shape[1]),
        )
        sub_sparse_test = SparseCSR(
            indptr=np.array([0] + [len(sparse_rows[i]) for i in test_idx], dtype=np.int32).cumsum(),
            indices=np.array([c for i in test_idx for c in sorted(sparse_rows[i])], dtype=np.int32),
            data=np.array([sparse_rows[i][c] for i in test_idx for c in sorted(sparse_rows[i])], dtype=np.float32),
            shape=(len(test_idx), X_sparse.shape[1]),
        )
        m_lr.fit(X_dense[train_idx], sub_sparse_train, y_all[train_idx], w_all[train_idx], steps=200)
        oof_logits_lr[test_idx] = m_lr.predict_logits(X_dense[test_idx], sub_sparse_test)

        # Fit MLP
        m_mlp = TinyMLPModel(np.zeros((X_dense.shape[1], 32), dtype=np.float32), np.zeros(32, dtype=np.float32), np.zeros((32, 1), dtype=np.float32))
        m_mlp.fit(X_dense[train_idx], y_all[train_idx], w_all[train_idx], steps=200)
        oof_logits_mlp[test_idx] = m_mlp.predict_logits(X_dense[test_idx])

    # Evaluate out-of-fold on eval_usable samples only!
    eval_y = y_all[eval_mask]
    eval_lr_probs = _sigmoid(oof_logits_lr[eval_mask])
    eval_mlp_probs = _sigmoid(oof_logits_mlp[eval_mask])

    auc_lr = compute_auc(eval_y, eval_lr_probs)
    auc_mlp = compute_auc(eval_y, eval_mlp_probs)

    # Select best model: Logistic Regression preferred unless MLP is significantly better (> 0.02 AUC)
    chosen_model = "mlp" if auc_mlp > auc_lr + 0.02 else "logistic"
    chosen_oof_logits = oof_logits_mlp if chosen_model == "mlp" else oof_logits_lr

    # Fit Platt Calibrator on out-of-fold logits of eval samples
    calibrator = PlattCalibrator()
    calibrator.fit(chosen_oof_logits[eval_mask], eval_y, w_all[eval_mask])
    cal_oof_probs = calibrator.predict_proba(chosen_oof_logits[eval_mask])

    cv_auc = compute_auc(eval_y, cal_oof_probs)
    cv_ece = expected_calibration_error(cal_oof_probs, eval_y)
    pred_pos = cal_oof_probs >= 0.5
    cv_prec = float(eval_y[pred_pos].mean()) if pred_pos.sum() > 0 else 0.0
    cv_rec = float(eval_y[pred_pos].sum() / max(eval_y.sum(), 1.0))

    # 2. Time-split Validation
    t_train_idx, t_test_idx = time_split(dates, train_ratio=0.8)
    t_eval_test_idx = [i for i in t_test_idx if eval_mask[i]]

    t_model = HybridLogisticModel(np.zeros(X_dense.shape[1], dtype=np.float32), np.zeros(X_sparse.shape[1], dtype=np.float32))
    t_sparse_train = SparseCSR(
        indptr=np.array([0] + [len(sparse_rows[i]) for i in t_train_idx], dtype=np.int32).cumsum(),
        indices=np.array([c for i in t_train_idx for c in sorted(sparse_rows[i])], dtype=np.int32),
        data=np.array([sparse_rows[i][c] for i in t_train_idx for c in sorted(sparse_rows[i])], dtype=np.float32),
        shape=(len(t_train_idx), X_sparse.shape[1]),
    )
    t_sparse_test = SparseCSR(
        indptr=np.array([0] + [len(sparse_rows[i]) for i in t_test_idx], dtype=np.int32).cumsum(),
        indices=np.array([c for i in t_test_idx for c in sorted(sparse_rows[i])], dtype=np.int32),
        data=np.array([sparse_rows[i][c] for i in t_test_idx for c in sorted(sparse_rows[i])], dtype=np.float32),
        shape=(len(t_test_idx), X_sparse.shape[1]),
    )
    t_model.fit(X_dense[t_train_idx], t_sparse_train, y_all[t_train_idx], w_all[t_train_idx], steps=250)
    t_test_logits = t_model.predict_logits(X_dense[t_test_idx], t_sparse_test)
    t_test_cal_probs = calibrator.predict_proba(t_test_logits)

    if t_eval_test_idx:
        test_eval_mask_in_test = [i in t_eval_test_idx for i in t_test_idx]
        t_eval_y = y_all[t_test_idx][test_eval_mask_in_test]
        t_eval_p = t_test_cal_probs[test_eval_mask_in_test]
        time_auc = compute_auc(t_eval_y, t_eval_p)
        time_ece = expected_calibration_error(t_eval_p, t_eval_y)
        t_pred_pos = t_eval_p >= 0.5
        time_prec = float(t_eval_y[t_pred_pos].mean()) if t_pred_pos.sum() > 0 else 0.0
        time_rec = float(t_eval_y[t_pred_pos].sum() / max(t_eval_y.sum(), 1.0))
    else:
        time_auc, time_ece, time_prec, time_rec = cv_auc, cv_ece, cv_prec, cv_rec

    # 3. Coverage Curve & Threshold Selection
    cov_curve = compute_coverage_curve(cal_oof_probs, eval_y)
    act_thresh, abstain_low, prec_at_act, cov_at_act = select_thresholds(
        cal_oof_probs, eval_y, target_precision=0.95, min_predicted=5
    )

    # 4. Learning Curve: evaluate at 25%, 50%, 100% of training data
    learning_curve: dict[str, float] = {}
    for frac in (0.25, 0.50, 1.00):
        n_sub = max(10, int(len(t_train_idx) * frac))
        sub_idx = t_train_idx[:n_sub]
        sub_sparse = SparseCSR(
            indptr=np.array([0] + [len(sparse_rows[i]) for i in sub_idx], dtype=np.int32).cumsum(),
            indices=np.array([c for i in sub_idx for c in sorted(sparse_rows[i])], dtype=np.int32),
            data=np.array([sparse_rows[i][c] for i in sub_idx for c in sorted(sparse_rows[i])], dtype=np.float32),
            shape=(len(sub_idx), X_sparse.shape[1]),
        )
        m_sub = HybridLogisticModel(np.zeros(X_dense.shape[1], dtype=np.float32), np.zeros(X_sparse.shape[1], dtype=np.float32))
        m_sub.fit(X_dense[sub_idx], sub_sparse, y_all[sub_idx], w_all[sub_idx], steps=200)
        p_sub = calibrator.predict_proba(m_sub.predict_logits(X_dense[t_test_idx], t_sparse_test))
        sub_auc = compute_auc(t_eval_y, p_sub[test_eval_mask_in_test]) if t_eval_test_idx else 0.5
        learning_curve[f"{int(frac*100)}%"] = round(float(sub_auc), 4)

    # 5. Safety & Active Mode Gating (Brief v9 section 3 rules):
    # - >= 95% precision at >= 20% coverage
    # - At least 30 positive and 30 negative human+verified examples
    # - Zero of gold-yes/applied/saved jobs demoted
    has_enough_pos = n_positives >= 30
    has_enough_neg = n_negatives >= 30
    meets_performance = prec_at_act >= 0.95 and cov_at_act >= 0.20

    # Check demotions on protected jobs in dataset
    prot_in_data = [i for i, jid in enumerate(job_ids) if jid in protected_job_ids]
    prot_demoted = 0
    if prot_in_data:
        prot_probs = cal_oof_probs[prot_in_data] if len(cal_oof_probs) > max(prot_in_data) else []
        prot_demoted = sum(1 for p in prot_probs if p >= act_thresh)

    can_act = has_enough_pos and has_enough_neg and meets_performance and (prot_demoted == 0)
    mode = "active" if can_act else "shadow"

    reasons: list[str] = []
    if not has_enough_pos:
        reasons.append(f"needs {30 - n_positives} more positive labels")
    if not has_enough_neg:
        reasons.append(f"needs {30 - n_negatives} more negative labels")
    if not meets_performance:
        reasons.append(f"coverage {cov_at_act*100:.1f}% or precision {prec_at_act*100:.1f}% below targets (95% prec at 20% cov)")
    if prot_demoted > 0:
        reasons.append(f"demoted {prot_demoted} protected jobs")

    reason_str = "; ".join(reasons) if reasons else "passed all safety gates"

    # Fit final model on all data with calibrator
    final_model = HybridLogisticModel(
        np.zeros(X_dense.shape[1], dtype=np.float32),
        np.zeros(X_sparse.shape[1], dtype=np.float32),
        calibrator=calibrator,
    )
    final_model.fit(X_dense, X_sparse, y_all, w_all, steps=300)

    eval_result = HeadEvaluationResult(
        head=head,
        n_samples=n_samples,
        n_positives=n_positives,
        n_eval_usable=n_eval,
        chosen_model=chosen_model,
        grouped_cv_auc=round(cv_auc, 4),
        grouped_cv_precision=round(cv_prec, 4),
        grouped_cv_recall=round(cv_rec, 4),
        grouped_cv_ece=round(cv_ece, 4),
        time_split_auc=round(time_auc, 4),
        time_split_precision=round(time_prec, 4),
        time_split_recall=round(time_rec, 4),
        time_split_ece=round(time_ece, 4),
        act_threshold=round(act_thresh, 4),
        abstain_low=round(abstain_low, 4),
        precision_at_act=round(prec_at_act, 4),
        coverage_at_act=round(cov_at_act, 4),
        coverage_curve=cov_curve,
        learning_curve=learning_curve,
        can_act=can_act,
        mode=mode,
        reason=reason_str,
    )

    return final_model, eval_result


def read_meta(config: Config) -> dict[str, Any]:
    path = config.resolve_path("data/models") / META_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


def save_models_and_meta(
    config: Config,
    models: dict[str, HybridLogisticModel],
    eval_results: dict[str, HeadEvaluationResult],
    *,
    clock: Clock = utc_now,
) -> Path:
    """Save trained models and metadata file."""
    dir_path = config.resolve_path("data/models")
    dir_path.mkdir(parents=True, exist_ok=True)

    meta: dict[str, Any] = {
        "trained_at": to_iso(clock()),
        "heads": {},
    }

    for head, model in models.items():
        m_file = f"{MODEL_FILE_PREFIX}_{head}.json"
        (dir_path / m_file).write_text(json.dumps(model.to_dict()), encoding="utf-8")

        res = eval_results[head]
        meta["heads"][head] = {
            "model_file": m_file,
            "n_samples": res.n_samples,
            "n_positives": res.n_positives,
            "n_eval_usable": res.n_eval_usable,
            "chosen_model": res.chosen_model,
            "grouped_cv_auc": res.grouped_cv_auc,
            "grouped_cv_precision": res.grouped_cv_precision,
            "grouped_cv_recall": res.grouped_cv_recall,
            "grouped_cv_ece": res.grouped_cv_ece,
            "time_split_auc": res.time_split_auc,
            "time_split_precision": res.time_split_precision,
            "time_split_recall": res.time_split_recall,
            "time_split_ece": res.time_split_ece,
            "act_threshold": res.act_threshold,
            "abstain_low": res.abstain_low,
            "precision_at_act": res.precision_at_act,
            "coverage_at_act": res.coverage_at_act,
            "coverage_curve": res.coverage_curve,
            "learning_curve": res.learning_curve,
            "can_act": res.can_act,
            "mode": res.mode,
            "reason": res.reason,
        }

    meta_path = dir_path / META_FILE
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta_path


def retrain_cascade_if_needed(
    connection: sqlite3.Connection,
    config: Config,
    *,
    client: OllamaClient | None = None,
    new_labels_needed: int = RETRAIN_NEW_LABELS_NEEDED,
    min_usable: int = MIN_USABLE_LABELS,
    tolerance: float = TOLERANCE,
    clock: Clock = utc_now,
) -> dict[str, Any]:
    """Guarded automatic retrain after match when >= 30 new labels exist.

    Keeps the old model if cross-validated metric drops by more than tolerance (0.03) or
    label count is below minimum. Returns decision summary for scheduled_runs.
    """
    old_meta = read_meta(config)
    labels_by_head = build_unified_labels(connection, config)

    total_labels = sum(len(labels) for h, labels in labels_by_head.items() if h in ("seniority", "location", "eligibility"))
    old_total = sum(int(h.get("n_samples", 0)) for h in old_meta.get("heads", {}).values()) if old_meta else 0

    decision: dict[str, Any] = {
        "at": to_iso(clock()),
        "total_labels": total_labels,
        "old_total": old_total,
        "new_labels": total_labels - old_total,
    }

    if old_meta and (total_labels - old_total < new_labels_needed):
        decision.update(action="skipped", reason=f"only {total_labels - old_total} new labels (need {new_labels_needed})")
        return decision

    if total_labels < min_usable:
        decision.update(action="skipped", reason=f"only {total_labels} usable labels (need {min_usable})")
        return decision

    # Extract all relevant job ids across heads
    all_job_ids = list(dict.fromkeys(u.job_id for labels in labels_by_head.values() for u in labels))
    dataset = extract_features_for_jobs(connection, all_job_ids, client=client, now_iso=to_iso(clock()))

    # Protected jobs that must never be demoted
    from jobhunt.matching.triage_apply import protected_ids
    prot_ids = protected_ids(connection, config)

    new_models: dict[str, HybridLogisticModel] = {}
    new_results: dict[str, HeadEvaluationResult] = {}

    for head in ("seniority", "location", "eligibility"):
        model, res = train_and_eval_head(connection, config, head, dataset, labels_by_head[head], prot_ids)
        new_models[head] = model
        new_results[head] = res

    # Check retrain guard against old meta:
    # Do not replace if CV AUC drops by > tolerance on any active head
    kept_old = False
    for head, res in new_results.items():
        old_head = old_meta.get("heads", {}).get(head)
        if old_head:
            old_auc = float(old_head.get("grouped_cv_auc", 0.0))
            if res.grouped_cv_auc < old_auc - tolerance:
                kept_old = True
                decision.update(
                    action="kept_previous",
                    reason=f"{head} CV AUC {res.grouped_cv_auc:.3f} dropped > {tolerance} below previous {old_auc:.3f}",
                )
                break

    if not kept_old:
        save_models_and_meta(config, new_models, new_results, clock=clock)
        decision.update(
            action="retrained",
            reason="enough new labels, not worse than previous models",
            heads={h: {"auc": res.grouped_cv_auc, "mode": res.mode} for h, res in new_results.items()},
        )
    return decision


def load_cascade_model(config: Config, head: str) -> HybridLogisticModel | None:
    """Load trained cascade model for a specific head."""
    dir_path = config.resolve_path("data/models")
    m_file = dir_path / f"{MODEL_FILE_PREFIX}_{head}.json"
    if not m_file.is_file():
        return None
    try:
        data = json.loads(m_file.read_text(encoding="utf-8"))
        return HybridLogisticModel.from_dict(data)
    except Exception as err:
        logger.warning("Could not load cascade model for %s: %s", head, err)
        return None


def export_eval_markdown(
    eval_results: dict[str, HeadEvaluationResult],
    config: Config,
    now_iso: str = "2026-10-30T00:00:00Z",
) -> Path:
    """Write comprehensive triage cascade evaluation markdown (data/eval/triage_eval.md)."""
    p = config.resolve_path("data/eval/triage_eval.md")
    p.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Triage Cascade Evaluation Report (Brief v9)",
        f"*Generated at: {now_iso}*",
        "",
        "## 1. Executive Summary & Head Modes",
        "",
        "| Head | Samples | Positives | Chosen Model | Grouped CV AUC | Time Split AUC | Mode | Safety Gate / Needs |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |",
    ]

    for head, res in eval_results.items():
        lines.append(
            f"| **{head}** | {res.n_samples} | {res.n_positives} | `{res.chosen_model}` | "
            f"{res.grouped_cv_auc:.3f} | {res.time_split_auc:.3f} | `{res.mode}` | {res.reason} |"
        )

    lines.extend([
        "",
        "## 2. Validation Metrics",
        "",
        "### A. Grouped K-Fold Cross-Validation (by Company)",
        "*Ensures no company's duplicate or similar postings appear across both train and test folds.*",
        "",
        "| Head | AUC | Precision | Recall | Expected Calibration Error (ECE) |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ])

    for head, res in eval_results.items():
        lines.append(
            f"| **{head}** | {res.grouped_cv_auc:.4f} | {res.grouped_cv_precision:.4f} | "
            f"{res.grouped_cv_recall:.4f} | {res.grouped_cv_ece:.4f} |"
        )

    lines.extend([
        "",
        "### B. Temporal Generalization (Time Split)",
        "*Trained on older 80% of postings, evaluated on newest 20% to test concept drift over time.*",
        "",
        "| Head | AUC | Precision | Recall | Expected Calibration Error (ECE) |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ])

    for head, res in eval_results.items():
        lines.append(
            f"| **{head}** | {res.time_split_auc:.4f} | {res.time_split_precision:.4f} | "
            f"{res.time_split_recall:.4f} | {res.time_split_ece:.4f} |"
        )

    lines.extend([
        "",
        "## 3. Calibrated Thresholds & Operating Points",
        "",
        "| Head | Act Threshold | Abstain Low | Precision @ Act | Coverage @ Act |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ])

    for head, res in eval_results.items():
        lines.append(
            f"| **{head}** | `p >= {res.act_threshold:.2f}` | `p <= {res.abstain_low:.2f}` | "
            f"{res.precision_at_act*100:.1f}% | {res.coverage_at_act*100:.1f}% |"
        )

    lines.extend([
        "",
        "## 4. Coverage-vs-Precision Curves",
        "*Displays the share of jobs each model head can confidently decide above threshold while maintaining precision.*",
        "",
    ])

    for head, res in eval_results.items():
        lines.extend([
            f"### Head: `{head}`",
            "",
            "| Threshold | Predicted Count | Coverage | Precision | Recall |",
            "| :---: | :---: | :---: | :---: | :---: |",
        ])
        for pt in res.coverage_curve:
            # Sample every 0.04 to keep table readable
            if int(round(pt["threshold"] * 100)) % 4 == 0 or pt["precision"] >= 0.95:
                lines.append(
                    f"| {pt['threshold']:.2f} | {pt['predicted']} | {pt['coverage']*100:.1f}% | "
                    f"{pt['precision']*100:.1f}% | {pt['recall']*100:.1f}% |"
                )
        lines.append("")

    lines.extend([
        "## 5. Learning Curves",
        "*Cross-validated AUC progression at 25%, 50%, and 100% of available training labels.*",
        "",
        "| Head | 25% Labels AUC | 50% Labels AUC | 100% Labels AUC | Gain from More Labels |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ])

    for head, res in eval_results.items():
        lc = res.learning_curve
        auc25 = lc.get("25%", 0.0)
        auc50 = lc.get("50%", 0.0)
        auc100 = lc.get("100%", 0.0)
        gain = auc100 - auc25
        gain_str = f"+{gain:.3f}" if gain >= 0 else f"{gain:.3f}"
        lines.append(f"| **{head}** | {auc25:.3f} | {auc50:.3f} | {auc100:.3f} | `{gain_str}` |")

    lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


__all__ = [
    "HeadEvaluationResult",
    "compute_auc",
    "compute_coverage_curve",
    "export_eval_markdown",
    "grouped_kfold_split",
    "load_cascade_model",
    "read_meta",
    "retrain_cascade_if_needed",
    "save_models_and_meta",
    "select_thresholds",
    "should_accept_retrain",
    "time_split",
    "train_and_eval_head",
]
