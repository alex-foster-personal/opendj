"""Ratcheting ceiling: warm anlz median vs trailing 7-day ledger median."""

from __future__ import annotations

import datetime as dt

from scripts.perf.kpi_ceiling import ceiling_exceeded, trailing_median_ms


def _entry(date: str, kpi: str, value: float) -> dict:
    return {
        "date": date,
        "kpi": kpi,
        "value": value,
        "unit": "ms",
        "measured": True,
    }


def test_trailing_median_ignores_errors_and_superseded() -> None:
    """If the ledger holds error and superseded rows then the median uses only live numbers."""
    today = dt.date(2026, 9, 11)
    entries = [
        _entry("2026-09-05", "deck_load_anlz_warm_median_ms_small_mp3", 80.0),
        _entry("2026-09-08", "deck_load_anlz_warm_median_ms_small_mp3", 100.0),
        {
            "date": "2026-09-09",
            "kpi": "deck_load_anlz_warm_median_ms_small_mp3",
            "value": None,
            "measured": False,
            "status": "error",
        },
        {
            "date": "2026-09-10",
            "kpi": "deck_load_anlz_warm_median_ms_small_mp3",
            "value": 999.0,
            "note": "SUPERSEDED wrong run",
        },
        _entry("2026-09-10", "deck_load_anlz_warm_median_ms_small_mp3", 120.0),
    ]
    median = trailing_median_ms(
        entries,
        kpi="deck_load_anlz_warm_median_ms_small_mp3",
        today=today,
        window_days=7,
    )
    assert median == 100.0


def test_ceiling_passes_at_exactly_three_times() -> None:
    """If warm median equals 3x the trailing median then the ceiling does not fail."""
    assert ceiling_exceeded(warm_median_ms=300.0, trailing_median_ms=100.0, factor=3.0) is False


def test_ceiling_fails_above_three_times() -> None:
    """If warm median exceeds 3x the trailing median then the ceiling fails loud."""
    assert ceiling_exceeded(warm_median_ms=301.0, trailing_median_ms=100.0, factor=3.0) is True


def test_ceiling_fails_when_trailing_median_missing() -> None:
    """If no trailing median exists then the ceiling cannot be scored and fails closed."""
    assert ceiling_exceeded(warm_median_ms=50.0, trailing_median_ms=None, factor=3.0) is True
