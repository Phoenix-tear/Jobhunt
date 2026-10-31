"""Pure-Python Spearman helper checked against hand-computed values."""

from __future__ import annotations

import pytest

from jobhunt.matching.stats import average_ranks, spearman


def test_average_ranks_with_ties() -> None:
    assert average_ranks([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]
    assert average_ranks([5, 5, 5]) == [2.0, 2.0, 2.0]


def test_perfect_and_inverse_monotone() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [9, 7, 5, 1]) == pytest.approx(-1.0)


def test_hand_computed_without_ties() -> None:
    # y ranks 2,1,4,3,5 -> sum d^2 = 4 -> 1 - 6*4/(5*24) = 0.8
    assert spearman([1, 2, 3, 4, 5], [20, 10, 40, 30, 50]) == pytest.approx(0.8)


def test_hand_computed_with_ties() -> None:
    # x ranks 1, 2.5, 2.5, 4 ; y ranks 1, 2, 3, 4 -> Pearson = 0.9486832981
    assert spearman([1, 2, 2, 3], [1, 2, 3, 4]) == pytest.approx(0.9486832981, abs=1e-9)


def test_constant_series_is_none_not_nan() -> None:
    assert spearman([3, 3, 3], [1, 2, 3]) is None
    assert spearman([1, 2, 3], [7, 7, 7]) is None
    assert spearman([1], [1]) is None
    assert spearman([], []) is None
