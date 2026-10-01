"""PERFMODE-14 library mode KPI capture: sample-count floor and note checks.

Split out of test_library_mode_capture.py (the 600-line test-file ratchet)
when PR #4553 added the sample-count-separation tests below. Covers the
`_MIN_SCORED_SAMPLES` floor per (mode, field) and the note's per-field sample
counts; the rest of the capture's behavior (dry-run schema, Playwright exit
handling, settle/dwell/stable-id gating) stays in test_library_mode_capture.py.
"""

from __future__ import annotations

from typing import cast

from scripts.perf.capture_library_mode import (
    _MIN_SCORED_SAMPLES,
    _rows_from_capture_result,
)


def _mode(
    footprint_mb: float, cpu_percent: float, *, ticks: int, rss_mb: float | None = None
) -> dict[str, object]:
    rss = footprint_mb if rss_mb is None else rss_mb
    return {
        "footprint_samples_mb": [footprint_mb] * ticks,
        "cpu_samples_percent": [cpu_percent] * ticks,
        "rss_samples_mb": [rss] * ticks,
        "median_footprint_mb": footprint_mb,
        "median_cpu_percent": cpu_percent,
        "median_rss_mb": rss,
        "sample_failure_count": 0,
    }


def test_capture_library_mode_refuses_measured_below_the_absolute_sample_floor() -> None:
    """Codex P1/BLOCKING, PR #4034, discussion_r4138153190: the Playwright
    spec's own sample floor is relative to `expectedTicks`, which collapses
    to 1 when KPI_CAPTURE_SAMPLE_INTERVAL_S is set to the dwell length or
    longer -- a single reading would satisfy that floor and could still flip
    LIB-MODE/PERFMODE-14 to PASS without establishing steady-state behavior.
    This is the absolute floor: settle and dwell both conform and stable_ids
    is valid, but too few samples must still refuse to score."""
    for ticks in (1, 5):
        rows = _rows_from_capture_result(
            {
                "ok": True,
                "gig": _mode(1000.0, 10.0, ticks=ticks),
                "library": _mode(500.0, 3.0, ticks=ticks),
                "sampling_method": (
                    "CDP renderer+gpu ps rss/cpu plus engine family rss_mb, every 5s"
                ),
                "stable_ids": ["a" * 40, "b" * 40, "c" * 40, "d" * 40],
            },
            capture_id="issue-2700-library-mode-test",
            machine="testhost",
            app_build_sha="abc123",
            dwell_seconds=60,
            settle_seconds=60,
            frontend_mode="static-build",
        )
        for row in rows:
            assert row["measured"] is False
            assert "UNMEASURED" in row["note"]
            assert "insufficient samples" in row["note"]
            assert f"gig.footprint_samples_mb={ticks}" in row["note"]
            assert f"gig.cpu_samples_percent={ticks}" in row["note"]
            assert f"library.footprint_samples_mb={ticks}" in row["note"]
            assert f"library.cpu_samples_percent={ticks}" in row["note"]


def test_capture_library_mode_scores_measured_right_at_the_sample_floor() -> None:
    """The boundary: exactly `_MIN_SCORED_SAMPLES` per mode is enough."""
    rows = _rows_from_capture_result(
        {
            "ok": True,
            "gig": _mode(1000.0, 10.0, ticks=_MIN_SCORED_SAMPLES),
            "library": _mode(500.0, 3.0, ticks=_MIN_SCORED_SAMPLES),
            "sampling_method": "CDP renderer+gpu ps rss/cpu plus engine family rss_mb, every 5s",
            "stable_ids": ["a" * 40, "b" * 40, "c" * 40, "d" * 40],
        },
        capture_id="issue-2700-library-mode-test",
        machine="testhost",
        app_build_sha="abc123",
        dwell_seconds=60,
        settle_seconds=60,
        frontend_mode="static-build",
    )
    for row in rows:
        assert row["measured"] is True
        assert "UNMEASURED" not in row["note"]


def test_capture_library_mode_refuses_measured_when_only_cpu_samples_are_sparse() -> None:
    """Codex P1/BLOCKING, PR #4034, discussion at sha=09612c7a6f: the sample
    floor originally counted only `footprint_samples_mb` and used that count
    to stand in for `cpu_samples_percent` too. A capture whose CPU ticks
    dropped (fewer CPU readings landed than footprint readings, e.g. from a
    flakier `ps %cpu` read) must still refuse to score, even though its
    footprint sample count alone clears the floor."""
    sparse_cpu_mode = _mode(1000.0, 10.0, ticks=_MIN_SCORED_SAMPLES)
    sparse_cpu_mode["cpu_samples_percent"] = cast(
        "list[float]", sparse_cpu_mode["cpu_samples_percent"]
    )[:2]
    rows = _rows_from_capture_result(
        {
            "ok": True,
            "gig": sparse_cpu_mode,
            "library": _mode(500.0, 3.0, ticks=_MIN_SCORED_SAMPLES),
            "sampling_method": "CDP renderer+gpu ps rss/cpu plus engine family rss_mb, every 5s",
            "stable_ids": ["a" * 40, "b" * 40, "c" * 40, "d" * 40],
        },
        capture_id="issue-2700-library-mode-test",
        machine="testhost",
        app_build_sha="abc123",
        dwell_seconds=60,
        settle_seconds=60,
        frontend_mode="static-build",
    )
    for row in rows:
        assert row["measured"] is False
        assert "UNMEASURED" in row["note"]
        assert "insufficient samples" in row["note"]
        assert "gig.cpu_samples_percent=2" in row["note"]
        assert f"gig.footprint_samples_mb={_MIN_SCORED_SAMPLES}" in row["note"]


def test_capture_library_mode_note_records_cpu_sample_count_separately_from_footprint() -> None:
    """Sol P1/BLOCKING, PR #4553, discussion at capture_library_mode.py:381:
    the retained `samples_gig`/`samples_library` counts in the note always
    came from `footprint_samples_mb`, including on the
    `library_mode_cpu_ratio` row -- a result with 11 footprint samples and 9
    CPU samples passed every scoring gate but recorded the CPU median as
    resting on 11 samples too. Every (mode, field) count must be named
    independently, and the old collapsed `samples_gig=`/`samples_library=`
    keys must be gone, not merely supplemented, or a reader cannot tell
    which count backs which KPI."""
    gig = _mode(1000.0, 10.0, ticks=11)
    gig["cpu_samples_percent"] = cast("list[float]", gig["cpu_samples_percent"])[:9]
    library = _mode(500.0, 3.0, ticks=12)
    library["cpu_samples_percent"] = cast("list[float]", library["cpu_samples_percent"])[:7]

    rows = _rows_from_capture_result(
        {
            "ok": True,
            "gig": gig,
            "library": library,
            "sampling_method": "CDP renderer+gpu ps rss/cpu plus engine family rss_mb, every 5s",
            "stable_ids": ["a" * 40, "b" * 40, "c" * 40, "d" * 40],
        },
        capture_id="issue-2700-library-mode-test",
        machine="testhost",
        app_build_sha="abc123",
        dwell_seconds=60,
        settle_seconds=60,
        frontend_mode="static-build",
    )
    for row in rows:
        # Both CPU counts still clear _MIN_SCORED_SAMPLES (6), so this
        # mismatch alone must not block scoring -- it must be named, not
        # refused.
        assert row["measured"] is True
        assert "samples_gig_footprint=11" in row["note"]
        assert "samples_gig_cpu=9" in row["note"]
        assert "samples_library_footprint=12" in row["note"]
        assert "samples_library_cpu=7" in row["note"]
        assert "samples_gig=" not in row["note"]
        assert "samples_library=" not in row["note"]


def test_capture_library_mode_note_records_matching_counts_when_fields_agree() -> None:
    """Overshoot control for the fix above: when footprint and CPU sample
    counts genuinely agree (the common case), the separated note must still
    report both, equal, counts -- not regress into dropping one field."""
    rows = _rows_from_capture_result(
        {
            "ok": True,
            "gig": _mode(1000.0, 10.0, ticks=11),
            "library": _mode(500.0, 3.0, ticks=12),
            "sampling_method": "CDP renderer+gpu ps rss/cpu plus engine family rss_mb, every 5s",
            "stable_ids": ["a" * 40, "b" * 40, "c" * 40, "d" * 40],
        },
        capture_id="issue-2700-library-mode-test",
        machine="testhost",
        app_build_sha="abc123",
        dwell_seconds=60,
        settle_seconds=60,
        frontend_mode="static-build",
    )
    for row in rows:
        assert row["measured"] is True
        assert "samples_gig_footprint=11" in row["note"]
        assert "samples_gig_cpu=11" in row["note"]
        assert "samples_library_footprint=12" in row["note"]
        assert "samples_library_cpu=12" in row["note"]
