"""Regression tests for apps/lyrics/metrics.py (pure, no dataset needed).

- if score_onsets does not compute AAE/median/p95/within from hand-checked errors then broken
- if score_onsets accepts a ref/pred length mismatch then broken
- if score_onsets accepts empty or NaN input then broken
- if aggregate does not word-pool the mean exactly then broken
"""

from __future__ import annotations

import math

import pytest

from apps.lyrics.metrics import aggregate, score_onsets

# abs errors by construction: 0.0, 0.03, 0.26, 0.51 -- all far from tolerance boundaries
REF = [1.0, 2.0, 3.0, 4.0]
PRED = [1.0, 2.03, 3.26, 4.51]


def test_score_onsets_hand_checked_stats() -> None:
    report = score_onsets(REF, PRED)
    assert report.n_words == 4, "if n_words != 4 for 4 words then broken"
    assert report.mean_abs_error_s == pytest.approx(0.2), (
        "if AAE != mean(0,.03,.26,.51) then broken"
    )
    assert report.median_abs_error_s == pytest.approx(0.145), (
        "if median != (0.03+0.26)/2 then broken"
    )
    assert report.p95_abs_error_s == pytest.approx(0.51), "if p95 != max for n=4 then broken"
    assert report.within[0.05] == pytest.approx(2 / 4), "if within@50ms != 2/4 then broken"
    assert report.within[0.2] == pytest.approx(2 / 4), "if within@200ms != 2/4 then broken"
    assert report.within[0.3] == pytest.approx(3 / 4), "if within@300ms != 3/4 then broken"
    assert report.within[0.5] == pytest.approx(3 / 4), "if within@500ms != 3/4 then broken"


def test_score_onsets_rejects_length_mismatch() -> None:
    with pytest.raises(ValueError, match="every reference word"):
        score_onsets([1.0, 2.0], [1.0])


def test_score_onsets_rejects_empty_and_nan() -> None:
    with pytest.raises(ValueError, match="no words"):
        score_onsets([], [])
    with pytest.raises(ValueError, match="finite track second"):
        score_onsets([1.0], [math.nan])


def test_score_onsets_rejects_non_finite_and_negative_onsets() -> None:
    with pytest.raises(ValueError, match="finite track second"):
        score_onsets([1.0], [float("inf")])
    with pytest.raises(ValueError, match="finite track second"):
        score_onsets([1.0], [float("-inf")])
    with pytest.raises(ValueError, match="finite track second"):
        score_onsets([-0.1], [0.0])
    with pytest.raises(ValueError, match="finite track second"):
        score_onsets([0.0], [-0.1])
    score_onsets([0.0], [0.0])


def test_aggregate_pooled_quantiles_are_not_weighted_medians() -> None:
    zeros = score_onsets([0.0] * 100, [0.0] * 100)
    catastrophe = score_onsets([0.0], [100.0])
    pooled = aggregate([zeros, catastrophe])
    assert pooled.n_words == 101
    assert pooled.median_abs_error_s == pytest.approx(0.0)
    assert pooled.p95_abs_error_s == pytest.approx(0.0)
    # n-weighted mean of per-song medians would be ~0.990; that must not return


def test_aggregate_word_pools_the_mean() -> None:
    a = score_onsets([1.0], [1.1])  # n=1, AAE ~0.1
    b = score_onsets([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])  # n=3, AAE 0.0
    pooled = aggregate([a, b])
    assert pooled.n_words == 4, "if pooled n != 1+3 then broken"
    assert pooled.mean_abs_error_s == pytest.approx(0.1 / 4), (
        "if pooled AAE != (0.1*1+0*3)/4 then broken"
    )
    with pytest.raises(ValueError, match="no reports"):
        aggregate([])
