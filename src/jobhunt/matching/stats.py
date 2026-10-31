"""Tiny statistics helpers (pure Python): no scipy dependency."""

from __future__ import annotations

import math
from collections.abc import Sequence


def average_ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks; tied values share the mean of the ranks they span."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        shared = (position + end) / 2 + 1
        for index in order[position:end + 1]:
            ranks[index] = shared
        position = end + 1
    return ranks


def pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    """None when either series is constant or shorter than two points (never NaN)."""
    count = len(left)
    if count != len(right) or count < 2:
        return None
    mean_left, mean_right = sum(left) / count, sum(right) / count
    covariance = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    spread_left = sum((a - mean_left) ** 2 for a in left)
    spread_right = sum((b - mean_right) ** 2 for b in right)
    if spread_left <= 1e-12 or spread_right <= 1e-12:
        return None
    return max(-1.0, min(1.0, covariance / math.sqrt(spread_left * spread_right)))


def spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Spearman rank correlation (average ranks for ties, then Pearson); None for a constant series."""
    if len(left) != len(right) or any(math.isnan(v) for v in (*left, *right)):
        return None
    return pearson(average_ranks(left), average_ranks(right))


def spearman_or_nan(left: Sequence[float], right: Sequence[float]) -> float:
    """For benchmark scripts that print numbers: NaN (not an exception) when undefined."""
    value = spearman(list(left), list(right))
    return float("nan") if value is None else value
