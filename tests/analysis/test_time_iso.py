"""UTC ISO helpers for analysis persistence."""

from __future__ import annotations

from datetime import UTC, datetime

from apps.analysis.time_iso import datetime_to_utc_iso, utc_now_iso


def test_utc_now_iso_ends_with_z() -> None:
    assert utc_now_iso().endswith("Z")
    assert "+00:00" not in utc_now_iso()


def test_datetime_to_utc_iso_naive_and_aware() -> None:
    naive = datetime(2024, 1, 2, 3, 4, 5)
    assert datetime_to_utc_iso(naive) == "2024-01-02T03:04:05Z"
    aware = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert datetime_to_utc_iso(aware) == "2024-01-02T03:04:05Z"
