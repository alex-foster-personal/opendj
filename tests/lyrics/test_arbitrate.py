"""Regression tests for apps/lyrics/arbitrate.py (pure, synthetic fixture).

- if extract_span_features stops computing score/wps contrasts correctly then broken
- if a whole-song span or out-of-range span is accepted then broken
- if a zero-duration (collapsed) span produces infinite wps then broken
"""

from __future__ import annotations

import pytest

from apps.lyrics.arbitrate import extract_span_features


def _word(start: float, dur: float, score: float) -> dict:
    return {"word": "x", "start_s": start, "end_s": start + dur, "score": score}


def test_features_hand_checked() -> None:
    # 4 healthy words (0.5s each, score -0.1) then a 2-word suspect span
    # (0.1s each, score -0.9): crammed and low-confidence.
    aligned = [_word(i * 0.6, 0.5, -0.1) for i in range(4)]
    aligned += [_word(2.4 + i * 0.1, 0.1, -0.9) for i in range(2)]
    f = extract_span_features(aligned, 4, 5)
    assert f.n_words == 2
    assert f.span_mean_score == pytest.approx(-0.9), "if span mean != -0.9 then broken"
    assert f.rest_median_score == pytest.approx(-0.1), "if rest median != -0.1 then broken"
    assert f.score_delta == pytest.approx(-0.8), "if delta != span - rest then broken"
    assert f.span_wps == pytest.approx(2 / 0.2), "if span wps != 10 then broken"
    assert f.rest_wps == pytest.approx(4 / 2.0), "if rest wps != 2 then broken"
    assert f.wps_ratio == pytest.approx(5.0), "if crammed span is not wps_ratio 5 then broken"


def test_collapsed_span_stays_finite() -> None:
    aligned = [_word(i * 0.6, 0.5, -0.1) for i in range(3)] + [_word(1.8, 0.0, -1.0)]
    f = extract_span_features(aligned, 3, 3)
    assert f.span_wps < 1e5, "if a zero-duration span yields infinite wps then broken"


def test_bad_spans_fail_fast() -> None:
    aligned = [_word(0, 0.5, -0.1), _word(0.6, 0.5, -0.1)]
    with pytest.raises(ValueError, match="whole song"):
        extract_span_features(aligned, 0, 1)
    with pytest.raises(ValueError, match="out of range"):
        extract_span_features(aligned, 1, 2)
