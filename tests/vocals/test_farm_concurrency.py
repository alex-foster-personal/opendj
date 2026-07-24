"""Regression tests for OBSERVED container concurrency.

The ledger recorded ``max_parallel_gpus: 10`` for seven snapshots, including a
one-track run where 10 is impossible, because a configured cap was written into
a measurement slot. These tests exist so the replacement cannot regress into
the same failure mode: every number here comes from spans, never from a cap.

Single-line intent, in the repo's regression style:
  - if serialised calls do not score mean 1.0 then the metric cannot detect a failed fan-out
  - if simultaneous calls do not score mean N then the metric understates real fan-out
  - if peak counts a call that ended as another started then overlap is overcounted
  - if the 128-track numbers do not reproduce 1.05 then the metric contradicts the evidence
  - if a missing wall_span is tolerated then a stale container silently flatters the number
  - if a backwards span is accepted then a clock fault is reported as a measurement
"""
from __future__ import annotations

import pytest

from scripts.farm_concurrency import (
    concurrency_profile,
    implied_max_containers,
    span_of,
    summarise,
)


def test_serialised_calls_score_one() -> None:
    """if serialised calls do not score 1.0 then a failed fan-out is invisible"""
    spans = [(float(i * 10), float(i * 10 + 10)) for i in range(10)]
    profile = concurrency_profile(spans)
    assert profile["peak"] == 1.0
    assert profile["mean"] == 1.0
    assert profile["calls"] == 10.0


def test_simultaneous_calls_score_the_full_width() -> None:
    """if simultaneous calls do not score N then real fan-out is understated"""
    spans = [(0.0, 10.0)] * 10
    profile = concurrency_profile(spans)
    assert profile["peak"] == 10.0
    assert profile["mean"] == 10.0
    assert profile["busy_window_s"] == 10.0


def test_touching_spans_do_not_overlap() -> None:
    """if peak counts a call ending as another starts then overlap is overcounted"""
    assert concurrency_profile([(0.0, 5.0), (5.0, 10.0)])["peak"] == 1.0
    assert concurrency_profile([(0.0, 5.0), (4.999, 10.0)])["peak"] == 2.0


def test_partial_overlap_is_measured_not_rounded() -> None:
    """if partial overlap is mismeasured then staged runs cannot be compared"""
    # Three calls, each 10s, starting 5s apart: 25s window, 30 container-s.
    profile = concurrency_profile([(0.0, 10.0), (5.0, 15.0), (10.0, 20.0)])
    assert profile["peak"] == 2.0
    assert profile["busy_window_s"] == 20.0
    assert profile["mean"] == 1.5


def test_reproduces_the_128_track_finding() -> None:
    """if the 128-track numbers do not reproduce 1.05 then the metric is wrong"""
    # 954 container-seconds over a 909s wall is the arithmetic that falsified
    # the "10-way fan-out" claim. Model it as 128 calls of 7.45s laid nearly
    # end to end across a 909s window.
    calls = 128
    container_s = 954.0 / calls
    stride = 909.0 / calls
    spans = [(i * stride, i * stride + container_s) for i in range(calls)]
    profile = concurrency_profile(spans)
    assert profile["mean"] == pytest.approx(1.05, abs=0.02)
    assert profile["peak"] <= 2.0


def test_empty_is_zero_not_a_cap() -> None:
    """if an empty run reported a cap then config would leak into a measurement"""
    profile = concurrency_profile([])
    assert profile == {"peak": 0.0, "mean": 0.0, "busy_window_s": 0.0, "calls": 0.0}


def test_backwards_span_is_a_fault() -> None:
    """if a backwards span is accepted then a clock fault reads as a measurement"""
    with pytest.raises(ValueError, match="ends before it starts"):
        concurrency_profile([(10.0, 5.0)])


def test_missing_span_raises() -> None:
    """if a missing wall_span is tolerated then a stale container flatters the number"""
    with pytest.raises(RuntimeError, match="carries no wall_span"):
        span_of({"stable_id": "abc"})


def test_malformed_span_raises() -> None:
    """if a malformed span is accepted then concurrency is computed from junk"""
    with pytest.raises(RuntimeError, match="malformed"):
        span_of({"stable_id": "abc", "wall_span": [1.0]})


def test_span_of_reads_the_pair() -> None:
    """if span_of mangles the pair then every downstream number is wrong"""
    assert span_of({"stable_id": "x", "wall_span": [3, 9]}) == (3.0, 9.0)


def test_implied_ceiling_is_littles_law() -> None:
    """if the feeder ceiling is miscomputed then the benchmark proves nothing"""
    # One input every 0.77s against a 7.45s container: 9.68 is unreachable, so
    # the SERIAL feeder's real ceiling is what the measured rate says it is.
    assert implied_max_containers(1 / 0.77, 7.45) == pytest.approx(9.68, abs=0.02)
    assert implied_max_containers(0.0, 7.45) == 0.0
    with pytest.raises(ValueError):
        implied_max_containers(-1.0, 7.45)


def test_summary_names_the_cap_as_configured() -> None:
    """if the summary blurs observed and configured then the old bug returns"""
    line = summarise([(0.0, 10.0), (0.0, 10.0)], configured_cap=10)
    assert "observed peak 2" in line
    assert "cap configured at 10" in line
