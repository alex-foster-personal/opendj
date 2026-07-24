"""Regression tests for OBSERVED container concurrency.

The ledger recorded ``max_parallel_gpus: 10`` for seven snapshots, including a
one-track run where 10 is impossible, because a configured cap was written into
a measurement slot. These tests exist so the replacement cannot regress into
the same failure mode: every number here comes from spans, never from a cap.

Single-line intent, in the repo's regression style:
  - if serialised calls do not score mean 1.0 then the metric cannot detect a failed fan-out
  - if simultaneous calls do not score mean N then the metric understates real fan-out
  - if peak counts a call that ended as another started then overlap is overcounted
  - if a nearly-serial run does not score ~1 then a failed fan-out reads as healthy
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


def test_serial_shaped_run_scores_near_one() -> None:
    """if a nearly-serial run does not score ~1 then a failed fan-out reads as fine"""
    # Shaped from the arithmetic that first raised the alarm on the 128-track
    # run: 954 container-seconds over a claimed 909s wall. NOTE that claim did
    # not survive: kpi_derive later recomputed the same run from its 118 cache
    # entries and got a 178.5s publication window, i.e. mean concurrency 5.34,
    # because per_track_wall_s 7.1 was hand-entered and reproduces no artifact.
    # The case is kept because the SHAPE is what matters here -- calls laid end
    # to end must score ~1 whatever run inspired the numbers.
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


#----- producer/consumer contract ---------------------------------------------
# The farm STAMPS wall_span in the container; apps.vocals.cache PERSISTS it;
# scripts/bench/kpi_derive READS it to fill observed_peak_concurrency. Three
# files, three owners, one contract -- and it is that contract which makes the
# ledger's peak figure derivable at all, so it gets a test of its own.


def _worker_result(span: list[float]) -> dict:
    """A separate_track return value, shaped as _write_result hands it on."""
    return {
        "schema": 2,
        "source": "demucs-htdemucs",
        "fps": 2.0,
        "duration_s": 180.0,
        "coverage_pct": 42.0,
        "regions": [{"start_s": 1.0, "end_s": 9.0, "confidence": 0.8}],
        "device": "cuda",
        "source_sample_rate": 44100,
        "analysis_sample_rate": 44100,
        "wall_span": span,
        "params": {"model": "htdemucs"},
        "timings": {"load_s": 0.4, "separate_s": 5.6, "container_s": 8.1},
    }


def test_wall_span_survives_into_the_cache_entry(tmp_path) -> None:
    """if the farm's wall_span is dropped at write time then peak stays underivable"""
    from apps.vocals import cache as vcache

    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"not really audio")
    entry = vcache.write_entry(
        tmp_path / "cache" / "abc123.json",
        _worker_result([1000.0, 1008.1]),
        audio,
        preset={"tag": "t"},
    )
    assert entry["worker"]["wall_span"] == [1000.0, 1008.1]


def test_kpi_derive_reads_the_span_the_farm_stamped(tmp_path) -> None:
    """if kpi_derive cannot read the farm's span then the two ends disagree"""
    from apps.vocals import cache as vcache
    from scripts.bench.kpi_derive import _wall_span

    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"not really audio")
    entry = vcache.write_entry(
        tmp_path / "cache" / "abc123.json",
        _worker_result([2000.0, 2008.1]),
        audio,
        preset={"tag": "t"},
    )
    assert _wall_span(entry["worker"], "abc123.json") == (2000.0, 2008.1)


def test_farm_and_ledger_sweep_lines_agree() -> None:
    """if the two sweep-line implementations diverge then run and ledger disagree"""
    from scripts.bench.kpi_derive import sweep_peak

    spans = [(0.0, 10.0), (5.0, 15.0), (10.0, 20.0), (1.0, 2.0)]
    assert concurrency_profile(spans)["peak"] == float(sweep_peak(spans))
