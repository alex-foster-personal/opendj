"""Unit tests for capture_library_mode's scored-settle enforcement (PERFMODE-14).

Sol review, PR #4034, discussion_r4128465144: a --settle-seconds below the
required 60 s post-switch settling period must never enter the scorecard as
release evidence. `_rows_from_capture_result` always defaulted `measured` to
True regardless of settle_seconds, so a debug run could silently score.

Codex review, PR #4034 (2026-09-29): `measured` checked settle_seconds alone,
so `--settle-seconds 60 --dwell-seconds 10` still scored -- a one-second dwell
could enter the scorecard as valid PERFMODE-14 evidence as long as the settle
period conformed. Both periods must meet the scoring floor.

Sol P1/BLOCKING, PR #4034, discussion_r4138211284: this fixture originally had
only three samples per mode and no `stable_ids`, while `_rows_from_capture_result`
now also requires >=6 samples per mode (`_MIN_SCORED_SAMPLES`) and exactly four
deck ids (`_valid_deck_stable_ids`) before scoring measured. Every row was
therefore unmeasured regardless of settle/dwell, so the positive tests
(expecting `measured is True`) were failing for an unrelated reason, and the
negative settle/dwell tests could pass without actually exercising their
stated subject. The fixture now supplies six samples and four valid ids so
only settle_seconds/dwell_seconds (the thing each test varies) controls the
verdict.
"""

from __future__ import annotations

from typing import Any

from scripts.perf import capture_library_mode as clm


def _capture_result() -> dict[str, Any]:
    return {
        "ok": True,
        "sampling_method": "phys_footprint",
        "stable_ids": ["a" * 40, "b" * 40, "c" * 40, "d" * 40],
        "gig": {
            "median_footprint_mb": 1000.0,
            "median_cpu_percent": 10.0,
            "median_rss_mb": 900.0,
            "footprint_samples_mb": [980.0, 990.0, 1000.0, 1000.0, 1010.0, 1020.0],
            "cpu_samples_percent": [8.0, 9.0, 10.0, 10.0, 11.0, 12.0],
        },
        "library": {
            "median_footprint_mb": 500.0,
            "median_cpu_percent": 3.0,
            "median_rss_mb": 450.0,
            "footprint_samples_mb": [480.0, 490.0, 500.0, 500.0, 510.0, 520.0],
            "cpu_samples_percent": [2.0, 2.5, 3.0, 3.0, 3.5, 4.0],
        },
    }


def test_a_60s_settle_capture_scores_as_measured() -> None:
    rows = clm._rows_from_capture_result(
        _capture_result(),
        capture_id="perf-capture-test-1",
        machine="test-machine",
        app_build_sha="deadbeef",
        dwell_seconds=60,
        settle_seconds=60,
        frontend_mode="static-build",
    )
    assert len(rows) == 2
    for row in rows:
        assert row["measured"] is True, "a conforming 60s settle must score as measured"


def test_a_shortened_settle_capture_is_marked_unmeasured() -> None:
    rows = clm._rows_from_capture_result(
        _capture_result(),
        capture_id="perf-capture-test-2",
        machine="test-machine",
        app_build_sha="deadbeef",
        dwell_seconds=60,
        settle_seconds=0,
        frontend_mode="static-build",
    )
    assert len(rows) == 2
    for row in rows:
        assert row["measured"] is False, "settle_seconds=0 must never score as release evidence"
        assert "UNMEASURED" in row["note"]
        assert "settle_seconds=0" in row["note"]


def test_a_settle_one_second_under_the_floor_is_still_unmeasured() -> None:
    rows = clm._rows_from_capture_result(
        _capture_result(),
        capture_id="perf-capture-test-3",
        machine="test-machine",
        app_build_sha="deadbeef",
        dwell_seconds=60,
        settle_seconds=59,
        frontend_mode="static-build",
    )
    for row in rows:
        assert row["measured"] is False, "59s is still below the 60s floor"


def test_a_conforming_settle_with_a_shortened_dwell_is_still_unmeasured() -> None:
    """Codex P1, PR #4034: settle alone is not the whole floor.

    A 60s --settle-seconds with a 10s --dwell-seconds previously scored,
    because the measured expression checked only settle_seconds. Both
    periods must meet the floor or the run is not release evidence.
    """
    rows = clm._rows_from_capture_result(
        _capture_result(),
        capture_id="perf-capture-test-4",
        machine="test-machine",
        app_build_sha="deadbeef",
        dwell_seconds=10,
        settle_seconds=60,
        frontend_mode="static-build",
    )
    assert len(rows) == 2
    for row in rows:
        assert row["measured"] is False, "a 10s dwell must never score, even with a 60s settle"
        assert "UNMEASURED" in row["note"]
        assert "dwell_seconds=10" in row["note"]


def test_a_dwell_one_second_under_the_floor_is_still_unmeasured() -> None:
    rows = clm._rows_from_capture_result(
        _capture_result(),
        capture_id="perf-capture-test-5",
        machine="test-machine",
        app_build_sha="deadbeef",
        dwell_seconds=59,
        settle_seconds=60,
        frontend_mode="static-build",
    )
    for row in rows:
        assert row["measured"] is False, "a 59s dwell is still below the 60s floor"


def test_both_periods_at_the_floor_score_as_measured() -> None:
    rows = clm._rows_from_capture_result(
        _capture_result(),
        capture_id="perf-capture-test-6",
        machine="test-machine",
        app_build_sha="deadbeef",
        dwell_seconds=60,
        settle_seconds=60,
        frontend_mode="static-build",
    )
    for row in rows:
        assert row["measured"] is True, "60s dwell and 60s settle both meet the floor"
