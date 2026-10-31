"""Numpy models and calibration for the triage cascade (numpy only, no scipy / sklearn).

Includes:
1. L2-regularised Logistic Regression with sample weights and class weights, supporting hybrid dense + sparse features.
2. Tiny one-hidden-layer MLP (numpy) as an optional comparison.
3. Platt scaling and Isotonic regression (PAVA) fitted on out-of-fold predictions.
4. Calibration evaluation (Expected Calibration Error - ECE, Brier score).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from jobhunt.matching.triage_features import SparseCSR

logger = logging.getLogger(__name__)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30.0, 30.0)))


@dataclass
class PlattCalibrator:
    """Platt scaling (1D logistic regression on logits)."""

    a: float = 1.0
    b: float = 0.0

    def fit(self, logits: np.ndarray, labels: np.ndarray, weights: np.ndarray | None = None, steps: int = 150) -> None:
        z = logits.astype(np.float64)
        y = labels.astype(np.float64)
        w = weights.astype(np.float64) if weights is not None else np.ones_like(y)
        w_norm = w / (w.sum() or 1.0)

        a, b = 1.0, 0.0
        lr = 0.1
        for _ in range(steps):
            p = _sigmoid(a * z + b)
            err = (p - y) * w_norm
            grad_a = np.sum(err * z)
            grad_b = np.sum(err)
            a -= lr * grad_a
            b -= lr * grad_b
        self.a = float(a)
        self.b = float(b)

    def predict_proba(self, logits: np.ndarray) -> np.ndarray:
        return _sigmoid(self.a * logits.astype(np.float64) + self.b)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "platt", "a": self.a, "b": self.b}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlattCalibrator":
        return cls(a=float(d.get("a", 1.0)), b=float(d.get("b", 0.0)))


@dataclass
class IsotonicCalibrator:
    """Pool Adjacent Violators Algorithm (PAVA) in pure numpy."""

    x_thresholds: list[float] = field(default_factory=list)
    y_values: list[float] = field(default_factory=list)

    def fit(self, scores: np.ndarray, labels: np.ndarray, weights: np.ndarray | None = None) -> None:
        n = len(scores)
        if n == 0:
            return
        order = np.argsort(scores)
        s_sorted = scores[order].astype(np.float64)
        y_sorted = labels[order].astype(np.float64)
        w_sorted = weights[order].astype(np.float64) if weights is not None else np.ones(n, dtype=np.float64)

        # PAVA block representation: (sum_wy, sum_w, min_x, max_x)
        blocks = [[y_sorted[i] * w_sorted[i], w_sorted[i], s_sorted[i], s_sorted[i]] for i in range(n)]

        i = 0
        while i < len(blocks) - 1:
            mean_curr = blocks[i][0] / blocks[i][1]
            mean_next = blocks[i + 1][0] / blocks[i + 1][1]
            if mean_curr > mean_next:
                # Merge blocks
                blocks[i][0] += blocks[i + 1][0]
                blocks[i][1] += blocks[i + 1][1]
                blocks[i][3] = blocks[i + 1][3]
                del blocks[i + 1]
                if i > 0:
                    i -= 1
            else:
                i += 1

        self.x_thresholds = [b[3] for b in blocks]
        self.y_values = [float(np.clip(b[0] / b[1], 0.0, 1.0)) for b in blocks]

    def predict_proba(self, scores: np.ndarray) -> np.ndarray:
        if not self.x_thresholds:
            return scores
        return np.interp(scores, self.x_thresholds, self.y_values, left=self.y_values[0], right=self.y_values[-1])

    def to_dict(self) -> dict[str, Any]:
        return {"type": "isotonic", "x": self.x_thresholds, "y": self.y_values}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "IsotonicCalibrator":
        return cls(x_thresholds=list(d.get("x", [])), y_values=list(d.get("y", [])))


def expected_calibration_error(probs: np.ndarray, labels: np.ndarray, n_bins: int = 10) -> float:
    """Compute Expected Calibration Error (ECE) with 10 uniform bins."""
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    total = len(probs)
    if total == 0:
        return 0.0

    for i in range(n_bins):
        low, high = bins[i], bins[i + 1]
        mask = (probs >= low) & (probs <= high if i == n_bins - 1 else probs < high)
        count = int(mask.sum())
        if count > 0:
            bin_conf = float(probs[mask].mean())
            bin_acc = float(labels[mask].mean())
            ece += (count / total) * abs(bin_acc - bin_conf)
    return float(ece)


@dataclass
class HybridLogisticModel:
    """L2-regularized logistic regression over hybrid dense + sparse features."""

    w_dense: np.ndarray        # shape (D_dense,)
    w_sparse: np.ndarray       # shape (D_sparse,)
    bias: float = 0.0
    l2: float = 0.05
    calibrator: PlattCalibrator | IsotonicCalibrator | None = None

    def fit(
        self,
        X_dense: np.ndarray,
        X_sparse: SparseCSR,
        y: np.ndarray,
        sample_weights: np.ndarray | None = None,
        *,
        steps: int = 250,
        lr: float = 0.05,
    ) -> None:
        N, D_dense = X_dense.shape
        _, D_sparse = X_sparse.shape

        y_f32 = y.astype(np.float32)
        s_w = sample_weights.astype(np.float32) if sample_weights is not None else np.ones(N, dtype=np.float32)

        # Class balancing
        pos_mask = y_f32 >= 0.5
        neg_mask = ~pos_mask
        pos_weight = float(s_w[pos_mask].sum()) or 1.0
        neg_weight = float(s_w[neg_mask].sum()) or 1.0

        class_weights = np.where(pos_mask, N / (2.0 * pos_weight), N / (2.0 * neg_weight)).astype(np.float32)
        weights = s_w * class_weights
        norm_factor = float(weights.sum()) or 1.0

        w_d = np.zeros(D_dense, dtype=np.float32)
        w_s = np.zeros(D_sparse, dtype=np.float32)
        b = 0.0

        # Adam optimizer state for fast convergence
        m_d, v_d = np.zeros_like(w_d), np.zeros_like(w_d)
        m_s, v_s = np.zeros_like(w_s), np.zeros_like(w_s)
        m_b, v_b = 0.0, 0.0
        beta1, beta2, eps = 0.9, 0.999, 1e-8

        for t in range(1, steps + 1):
            logits = X_dense @ w_d + X_sparse.dot_vector(w_s) + b
            probs = _sigmoid(logits)
            error = (probs - y_f32) * weights

            grad_d = (X_dense.T @ error) / norm_factor + (self.l2 * w_d) / norm_factor
            grad_s = X_sparse.rdot_vector(error) / norm_factor + (self.l2 * w_s) / norm_factor
            grad_b = float(error.sum()) / norm_factor

            # Adam step
            m_d = beta1 * m_d + (1 - beta1) * grad_d
            v_d = beta2 * v_d + (1 - beta2) * (grad_d**2)
            m_s = beta1 * m_s + (1 - beta1) * grad_s
            v_s = beta2 * v_s + (1 - beta2) * (grad_s**2)
            m_b = beta1 * m_b + (1 - beta1) * grad_b
            v_b = beta2 * v_b + (1 - beta2) * (grad_b**2)

            m_d_hat = m_d / (1 - beta1**t)
            v_d_hat = v_d / (1 - beta2**t)
            m_s_hat = m_s / (1 - beta1**t)
            v_s_hat = v_s / (1 - beta2**t)
            m_b_hat = m_b / (1 - beta1**t)
            v_b_hat = v_b / (1 - beta2**t)

            w_d -= lr * m_d_hat / (np.sqrt(v_d_hat) + eps)
            w_s -= lr * m_s_hat / (np.sqrt(v_s_hat) + eps)
            b -= lr * m_b_hat / (np.sqrt(v_b_hat) + eps)

        self.w_dense = w_d
        self.w_sparse = w_s
        self.bias = float(b)

    def predict_logits(self, X_dense: np.ndarray, X_sparse: SparseCSR) -> np.ndarray:
        return X_dense @ self.w_dense + X_sparse.dot_vector(self.w_sparse) + self.bias

    def predict_proba(self, X_dense: np.ndarray, X_sparse: SparseCSR) -> np.ndarray:
        logits = self.predict_logits(X_dense, X_sparse)
        if self.calibrator is not None:
            return self.calibrator.predict_proba(logits)
        return _sigmoid(logits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "logistic",
            "w_dense": self.w_dense.tolist(),
            "w_sparse_indices": np.nonzero(self.w_sparse)[0].tolist(),
            "w_sparse_values": self.w_sparse[np.nonzero(self.w_sparse)].tolist(),
            "sparse_dim": len(self.w_sparse),
            "bias": self.bias,
            "l2": self.l2,
            "calibrator": self.calibrator.to_dict() if self.calibrator is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HybridLogisticModel":
        w_dense = np.array(d["w_dense"], dtype=np.float32)
        sparse_dim = int(d.get("sparse_dim", 65536))
        w_sparse = np.zeros(sparse_dim, dtype=np.float32)
        indices = d.get("w_sparse_indices", [])
        values = d.get("w_sparse_values", [])
        if indices and values:
            w_sparse[indices] = values

        cal = None
        cal_dict = d.get("calibrator")
        if cal_dict:
            if cal_dict.get("type") == "platt":
                cal = PlattCalibrator.from_dict(cal_dict)
            elif cal_dict.get("type") == "isotonic":
                cal = IsotonicCalibrator.from_dict(cal_dict)

        return cls(w_dense=w_dense, w_sparse=w_sparse, bias=float(d["bias"]), l2=float(d.get("l2", 0.05)), calibrator=cal)


@dataclass
class TinyMLPModel:
    """One-hidden-layer MLP in pure numpy for non-linear comparison."""

    W1: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=np.float32))
    b1: np.ndarray = field(default_factory=lambda: np.zeros(1, dtype=np.float32))
    W2: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=np.float32))
    b2: float = 0.0
    hidden_dim: int = 32
    calibrator: PlattCalibrator | IsotonicCalibrator | None = None

    @classmethod
    def create(cls, input_dim: int, hidden_dim: int = 32) -> "TinyMLPModel":
        rng = np.random.RandomState(42)
        W1 = rng.randn(input_dim, hidden_dim).astype(np.float32) * np.sqrt(2.0 / input_dim)
        b1 = np.zeros(hidden_dim, dtype=np.float32)
        W2 = rng.randn(hidden_dim, 1).astype(np.float32) * np.sqrt(2.0 / hidden_dim)
        return cls(W1=W1, b1=b1, W2=W2, b2=0.0, hidden_dim=hidden_dim)

    def fit(
        self,
        X: np.ndarray,
        y: np.ndarray,
        sample_weights: np.ndarray | None = None,
        *,
        steps: int = 250,
        lr: float = 0.02,
    ) -> None:
        N, D_in = X.shape
        H = self.hidden_dim
        rng = np.random.RandomState(42)

        y_f32 = y.astype(np.float32).reshape(-1, 1)
        s_w = sample_weights.astype(np.float32).reshape(-1, 1) if sample_weights is not None else np.ones((N, 1), dtype=np.float32)
        norm_factor = float(s_w.sum()) or 1.0

        W1 = rng.randn(D_in, H).astype(np.float32) * np.sqrt(2.0 / D_in)
        b1 = np.zeros(H, dtype=np.float32)
        W2 = rng.randn(H, 1).astype(np.float32) * np.sqrt(2.0 / H)
        b2 = 0.0

        for _ in range(steps):
            # Forward
            z1 = X @ W1 + b1
            a1 = np.maximum(0.0, z1)  # ReLU
            z2 = a1 @ W2 + b2
            probs = _sigmoid(z2)

            # Backward
            err = (probs - y_f32) * s_w
            dW2 = (a1.T @ err) / norm_factor + 0.01 * W2
            db2 = float(err.sum()) / norm_factor

            da1 = err @ W2.T
            dz1 = da1 * (z1 > 0)
            dW1 = (X.T @ dz1) / norm_factor + 0.01 * W1
            db1 = dz1.sum(axis=0) / norm_factor

            W2 -= lr * dW2
            b2 -= lr * db2
            W1 -= lr * dW1
            b1 -= lr * db1

        self.W1 = W1
        self.b1 = b1
        self.W2 = W2
        self.b2 = float(b2)

    def predict_logits(self, X: np.ndarray) -> np.ndarray:
        z1 = X @ self.W1 + self.b1
        a1 = np.maximum(0.0, z1)
        return (a1 @ self.W2 + self.b2).ravel()

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        logits = self.predict_logits(X)
        if self.calibrator is not None:
            return self.calibrator.predict_proba(logits)
        return _sigmoid(logits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "mlp",
            "W1": self.W1.tolist(),
            "b1": self.b1.tolist(),
            "W2": self.W2.tolist(),
            "b2": self.b2,
            "hidden_dim": self.hidden_dim,
            "calibrator": self.calibrator.to_dict() if self.calibrator is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TinyMLPModel":
        cal = None
        cal_dict = d.get("calibrator")
        if cal_dict:
            if cal_dict.get("type") == "platt":
                cal = PlattCalibrator.from_dict(cal_dict)
            elif cal_dict.get("type") == "isotonic":
                cal = IsotonicCalibrator.from_dict(cal_dict)

        return cls(
            W1=np.array(d["W1"], dtype=np.float32),
            b1=np.array(d["b1"], dtype=np.float32),
            W2=np.array(d["W2"], dtype=np.float32),
            b2=float(d["b2"]),
            hidden_dim=int(d.get("hidden_dim", 32)),
            calibrator=cal,
        )


__all__ = [
    "HybridLogisticModel",
    "IsotonicCalibrator",
    "PlattCalibrator",
    "TinyMLPModel",
    "expected_calibration_error",
]
