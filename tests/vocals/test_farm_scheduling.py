"""Regression tests for batch ordering and the round-trip latency split.

Two independent defects motivated these. First, ``.starmap`` publishes in INPUT
order, so one 63.3-minute mix mid-batch held 8.8% of a run's container time and
stalled everything queued behind it. Second, every timer in the pipeline lived
INSIDE the container, leaving upload, queue wait, cold start, the trip home and
the ordering delay as one unmeasured residual that three rival hypotheses each
claimed.

Single-line intent, in the repo's regression style:
  - if the batch is not longest-first then one long track stalls everything behind it
  - if ordering is not deterministic then two runs of the same gap are not comparable
  - if duration is missing and size is ignored then a long untagged track still stalls
  - if the round trip does not decompose exactly then the residual is still unmeasured
  - if clock skew is clamped away then a skewed split reads as a real measurement
  - if the farm's prefetch constants drift from the module then flags lie about defaults
"""
from __future__ import annotations

import pytest

from scripts.modal_vocal_farm import (
    PREFETCH_DEPTH,
    PREFETCH_MAX_BYTES,
    PREFETCH_WORKERS,
    FarmTally,
    GapTrack,
    _record_latencies,
    sort_longest_first,
)


def _track(stable_id: str, duration_ms: int = 0, size: int = 0) -> GapTrack:
    from pathlib import Path

    return GapTrack(
        stable_id=stable_id,
        audio_path=Path(f"/tmp/{stable_id}.mp3"),
        duration_ms=duration_ms,
        size_bytes=size,
    )


def test_longest_track_goes_first() -> None:
    """if the batch is not longest-first then one long track stalls the rest"""
    batch = [
        _track("short", duration_ms=180_000),
        _track("themix", duration_ms=3_798_000),  # the real 63.3-minute mix
        _track("medium", duration_ms=600_000),
    ]
    assert [t.stable_id for t in sort_longest_first(batch)] == [
        "themix", "medium", "short",
    ]


def test_order_is_deterministic() -> None:
    """if ordering is not deterministic then two runs are not comparable"""
    batch = [_track(name, duration_ms=200_000) for name in ("c", "a", "b")]
    assert [t.stable_id for t in sort_longest_first(batch)] == ["a", "b", "c"]
    assert sort_longest_first(batch) == sort_longest_first(list(reversed(batch)))


def test_size_breaks_ties_when_duration_is_missing() -> None:
    """if duration is missing and size ignored then a long untagged track stalls"""
    batch = [
        _track("tagged", duration_ms=120_000, size=4_000_000),
        _track("untagged_big", duration_ms=0, size=54_800_000),
        _track("untagged_small", duration_ms=0, size=3_000_000),
    ]
    ordered = [t.stable_id for t in sort_longest_first(batch)]
    # A tagged duration always outranks an untagged row, but among untagged
    # rows the big file must still come before the small one.
    assert ordered[0] == "tagged"
    assert ordered.index("untagged_big") < ordered.index("untagged_small")


def test_round_trip_decomposes_exactly() -> None:
    """if the round trip does not decompose exactly then the residual survives"""
    tally = FarmTally()
    result = {"stable_id": "x", "wall_span": [1000.0, 1008.0], "timings": {}}
    _record_latencies(result, yielded_at=998.5, received_at=1010.25, tally=tally)

    timings = result["timings"]
    assert timings["dispatch_s"] == pytest.approx(1.5)
    assert timings["return_s"] == pytest.approx(2.25)
    assert timings["round_trip_s"] == pytest.approx(11.75)
    container_s = 1008.0 - 1000.0
    assert timings["dispatch_s"] + container_s + timings["return_s"] == pytest.approx(
        timings["round_trip_s"]
    )
    assert tally.clock_skew_calls == 0


def test_ordering_delay_shows_up_as_return_time() -> None:
    """if a buffered result does not show large return_s then head-of-line hides"""
    tally = FarmTally()
    # A short call that finished at once but was published 40s later, stuck
    # behind a longer call in starmap's ordering buffer.
    result = {"stable_id": "x", "wall_span": [100.0, 105.0], "timings": {}}
    _record_latencies(result, yielded_at=99.0, received_at=145.0, tally=tally)
    assert result["timings"]["return_s"] == pytest.approx(40.0)
    assert result["timings"]["dispatch_s"] == pytest.approx(1.0)


def test_clock_skew_is_counted_not_clamped() -> None:
    """if skew is clamped away then a skewed split reads as a real measurement"""
    tally = FarmTally()
    # Container clock ahead of the Mac: its start precedes our own yield.
    result = {"stable_id": "x", "wall_span": [90.0, 98.0], "timings": {}}
    _record_latencies(result, yielded_at=100.0, received_at=112.0, tally=tally)
    assert tally.clock_skew_calls == 1
    assert result["timings"]["dispatch_s"] < 0
    # The sum stays exact regardless of skew, which is the point.
    assert tally.round_trip_s == pytest.approx(12.0)


def test_farm_prefetch_constants_match_the_module() -> None:
    """if the farm's constants drift from the module then flags lie about defaults"""
    from apps.vocals import prefetch

    # Duplicated only because Modal imports the farm inside a container with no
    # `apps` package, so these cannot be imported at module scope there.
    assert PREFETCH_DEPTH == prefetch.DEFAULT_DEPTH
    assert PREFETCH_WORKERS == prefetch.DEFAULT_WORKERS
    assert PREFETCH_MAX_BYTES == prefetch.DEFAULT_MAX_BYTES


def test_serial_control_arm_is_reachable() -> None:
    """if depth 1 is rejected then the prefetch has no control arm to A/B against"""
    from pathlib import Path

    from apps.vocals.prefetch import read_ahead

    # Not a perf assertion, just that the serial configuration is legal.
    stream = read_ahead([Path(__file__)], depth=1, workers=1)
    assert next(stream).path == Path(__file__)
    stream.close()
