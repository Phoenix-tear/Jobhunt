"""Unit tests for triage features, models, calibration, and training (Brief v9 Phase 2)."""

from __future__ import annotations

import numpy as np
import pytest

from jobhunt.matching.triage_features import SparseCSR, extract_ngrams
from jobhunt.matching.triage_models import (
    HybridLogisticModel,
    IsotonicCalibrator,
    PlattCalibrator,
    TinyMLPModel,
    expected_calibration_error,
)
from jobhunt.matching.triage_train import (
    compute_coverage_curve,
    grouped_kfold_split,
    select_thresholds,
    should_accept_retrain,
    time_split,
)


def test_feature_hashing_determinism() -> None:
    """Verify that extract_ngrams is purely deterministic across multiple calls."""
    title = "Senior Staff Machine Learning Engineer"
    extract = "We are seeking a senior lead with 8+ years of python and pytorch experience."

    res1 = extract_ngrams(title, extract)
    res2 = extract_ngrams(title, extract)

    assert res1 == res2
    assert len(res1) > 0
    # Values should be L2-normalized
    norm = np.sqrt(sum(v * v for v in res1.values()))
    assert abs(norm - 1.0) < 1e-4


def test_sparse_csr_operations() -> None:
    """Verify pure-numpy SparseCSR dot_vector and rdot_vector match dense operations."""
    # Create 3 samples, 5 features
    dense_matrix = np.array(
        [
            [1.0, 0.0, 2.0, 0.0, 0.0],
            [0.0, 3.0, 0.0, 4.0, 0.0],
            [5.0, 0.0, 0.0, 0.0, 6.0],
        ],
        dtype=np.float32,
    )

    indptr = [0, 2, 4, 6]
    indices = [0, 2, 1, 3, 0, 4]
    data = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    sparse = SparseCSR(
        indptr=np.array(indptr, dtype=np.int32),
        indices=np.array(indices, dtype=np.int32),
        data=np.array(data, dtype=np.float32),
        shape=(3, 5),
    )

    w = np.array([0.5, -1.0, 2.0, 0.25, -0.5], dtype=np.float32)

    # 1. dot_vector: X @ w
    expected_dot = dense_matrix @ w
    actual_dot = sparse.dot_vector(w)
    np.testing.assert_allclose(actual_dot, expected_dot, rtol=1e-5)

    # 2. rdot_vector: X.T @ v
    v = np.array([1.5, -2.0, 0.5], dtype=np.float32)
    expected_rdot = dense_matrix.T @ v
    actual_rdot = sparse.rdot_vector(v)
    np.testing.assert_allclose(actual_rdot, expected_rdot, rtol=1e-5)


def test_hybrid_logistic_model_convergence() -> None:
    """Verify HybridLogisticModel trains and separates linearly separable data."""
    rng = np.random.default_rng(42)
    N = 100
    D_dense = 10
    D_sparse = 50

    # Synthetic separable data: feature 0 strongly positive
    X_dense = rng.normal(0, 1, size=(N, D_dense)).astype(np.float32)
    # Sparse dummy
    indptr = np.arange(0, N + 1, dtype=np.int32)
    indices = rng.integers(0, D_sparse, size=N, dtype=np.int32)
    data = np.ones(N, dtype=np.float32)
    X_sparse = SparseCSR(indptr=indptr, indices=indices, data=data, shape=(N, D_sparse))

    y = (X_dense[:, 0] > 0.0).astype(np.float32)

    model = HybridLogisticModel(
        w_dense=np.zeros(D_dense, dtype=np.float32),
        w_sparse=np.zeros(D_sparse, dtype=np.float32),
        l2=0.01,
    )
    model.fit(X_dense, X_sparse, y, steps=150, lr=0.1)

    preds = model.predict_proba(X_dense, X_sparse)
    acc = np.mean((preds >= 0.5) == (y >= 0.5))
    assert acc >= 0.90, f"Expected accuracy >= 0.90, got {acc}"

    # Verify serialization round-trip
    d = model.to_dict()
    loaded = HybridLogisticModel.from_dict(d)
    np.testing.assert_allclose(loaded.w_dense, model.w_dense)
    np.testing.assert_allclose(loaded.bias, model.bias)
    preds_loaded = loaded.predict_proba(X_dense, X_sparse)
    np.testing.assert_allclose(preds_loaded, preds)


def test_tiny_mlp_model() -> None:
    """Verify TinyMLPModel trains and predicts probabilities."""
    rng = np.random.default_rng(123)
    N = 80
    D = 10
    X = rng.normal(0, 1, size=(N, D)).astype(np.float32)
    y = ((X[:, 0] + X[:, 1]) > 0.0).astype(np.float32)

    mlp = TinyMLPModel.create(input_dim=D, hidden_dim=16)
    mlp.fit(X, y, steps=100, lr=0.05)
    probs = mlp.predict_proba(X)
    assert probs.shape == (N,)
    assert (probs >= 0.0).all() and (probs <= 1.0).all()

    # Serialization
    d = mlp.to_dict()
    reloaded = TinyMLPModel.from_dict(d)
    np.testing.assert_allclose(reloaded.predict_proba(X), probs, atol=1e-5)


def test_platt_and_isotonic_calibration() -> None:
    """Verify Platt and Isotonic calibrators and ECE calculation."""
    logits = np.array([-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0])
    labels = np.array([0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0])

    # Platt
    platt = PlattCalibrator()
    platt.fit(logits, labels)
    probs_platt = platt.predict_proba(logits)
    assert np.all(np.diff(probs_platt) >= 0)  # Monotonic
    assert probs_platt[0] < 0.2 and probs_platt[-1] > 0.8

    # Isotonic
    scores = np.array([0.1, 0.2, 0.35, 0.45, 0.7, 0.85, 0.95])
    iso = IsotonicCalibrator()
    iso.fit(scores, labels)
    probs_iso = iso.predict_proba(scores)
    assert np.all(np.diff(probs_iso) >= 0)  # Monotonic PAVA

    ece = expected_calibration_error(probs_iso, labels)
    assert 0.0 <= ece <= 1.0


def test_grouped_kfold_no_company_leakage() -> None:
    """Verify Grouped K-Fold split ensures no company appears in both train and test folds."""
    companies = [
        "Google", "Google", "Google",
        "Meta", "Meta",
        "Amazon", "Amazon", "Amazon", "Amazon",
        "Microsoft", "Microsoft",
        "Apple",
        "Netflix",
    ]
    folds = grouped_kfold_split(companies, n_splits=3, seed=42)
    assert len(folds) == 3

    all_test_indices = []
    for train_idx, test_idx in folds:
        train_cos = {companies[i] for i in train_idx}
        test_cos = {companies[i] for i in test_idx}
        overlap = train_cos.intersection(test_cos)
        assert len(overlap) == 0, f"Found overlapping companies between train and test: {overlap}"
        all_test_indices.extend(test_idx)

    # Every index should be tested exactly once
    assert sorted(all_test_indices) == list(range(len(companies)))


def test_time_split() -> None:
    """Verify time split puts newest 20% in test and older in train."""
    timestamps = [
        "2026-10-01T00:00:00Z",
        "2026-10-02T00:00:00Z",
        "2026-10-03T00:00:00Z",
        "2026-10-04T00:00:00Z",
        "2026-10-05T00:00:00Z",
        "2026-10-06T00:00:00Z",
        "2026-10-07T00:00:00Z",
        "2026-10-08T00:00:00Z",
        "2026-10-09T00:00:00Z",
        "2026-10-10T00:00:00Z",
    ]
    train_idx, test_idx = time_split(timestamps, test_ratio=0.2)
    assert len(train_idx) == 8
    assert len(test_idx) == 2
    # Verify temporal ordering
    assert max(train_idx) < min(test_idx)


def test_coverage_curve_and_threshold_selection() -> None:
    """Verify threshold selection targets >= 95% precision on positives."""
    probs = np.array([0.05, 0.1, 0.2, 0.6, 0.85, 0.92, 0.96, 0.98, 0.99])
    labels = np.array([0, 0, 0, 0, 0, 1, 1, 1, 1])

    curve = compute_coverage_curve(probs, labels)
    assert len(curve) > 0
    # Last few points should have 100% precision
    high_conf_point = [pt for pt in curve if pt["precision"] >= 0.95]
    assert len(high_conf_point) > 0

    act, abstain_low, prec, cov = select_thresholds(probs, labels, min_precision=0.95)
    # The act threshold should achieve >= 0.95 precision
    selected_mask = probs >= act
    if selected_mask.sum() > 0:
        precision = float(labels[selected_mask].mean())
        assert precision >= 0.95
    assert abstain_low < act


def test_retrain_guard_logic() -> None:
    """Verify retrain guard refuses degradation > 0.03 and low sample counts."""
    # 1. Low sample count rejects
    should_keep, reason = should_accept_retrain(
        old_metric=0.92,
        new_metric=0.95,
        new_label_count=15,
        min_labels=30,
        max_metric_drop=0.03,
    )
    assert not should_keep
    assert "insufficient labels" in reason

    # 2. Metric drop > 0.03 rejects
    should_keep, reason = should_accept_retrain(
        old_metric=0.92,
        new_metric=0.85,
        new_label_count=50,
        min_labels=30,
        max_metric_drop=0.03,
    )
    assert not should_keep
    assert "dropped by" in reason

    # 3. Metric within acceptable tolerance accepts
    should_keep, reason = should_accept_retrain(
        old_metric=0.92,
        new_metric=0.91,
        new_label_count=50,
        min_labels=30,
        max_metric_drop=0.03,
    )
    assert should_keep
    assert "accepted" in reason
