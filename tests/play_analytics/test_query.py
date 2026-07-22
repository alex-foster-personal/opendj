"""Acceptance tests for the read-only play-analytics query contract.

Requirements:
✔︎ A real Phase 12 SQLite event store produces deterministic session and play aggregates.
✔︎ A share-state filter is applied consistently to every projection.
✔︎ A missing or malformed store fails explicitly without creating data.

Acceptance tests:
[if] completed and active sessions contain track_loaded events [then ⛔️] totals or ordering differ from the fixed expected contract
[if] shared_local is requested [then ⛔️] private plays appear in any returned aggregate
[if] the database path or canonical schema is missing [then ⛔️] the query returns an empty success payload or creates a database
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.play_analytics.query import AnalyticsSchemaError, query_play_analytics


def test_query_returns_deterministic_session_history(analytics_db: Path) -> None:
    result = query_play_analytics(analytics_db, limit=50)

    assert result["schema_version"] == 1
    assert result["filters"] == {"share_state": None, "limit": 50}
    assert result["summary"] == {
        "sessions": 2,
        "plays": 4,
        "unique_tracks": 3,
        "completed_duration_s": 5400.0,
    }
    assert [row["session_id"] for row in result["sessions"]] == [
        "late-private",
        "early-shared",
    ]
    assert result["sessions"][0]["duration_s"] is None
    assert result["sessions"][0]["play_count"] == 2
    assert result["sessions"][1]["duration_s"] == 5400.0
    assert result["top_tracks"][0] == {
        "stable_id": "track-a",
        "title": "Alpha",
        "artist": "Artist One",
        "play_count": 2,
        "last_played_at": "2026-07-21T22:00:05+00:00",
    }
    assert [row["stable_id"] for row in result["top_tracks"]] == [
        "track-a",
        "track-c",
        "track-b",
    ]


def test_query_applies_share_state_filter_to_all_projections(
    analytics_db: Path,
) -> None:
    result = query_play_analytics(
        analytics_db,
        share_state="shared_local",
        limit=1,
    )

    assert result["filters"] == {"share_state": "shared_local", "limit": 1}
    assert result["summary"] == {
        "sessions": 1,
        "plays": 2,
        "unique_tracks": 2,
        "completed_duration_s": 5400.0,
    }
    assert [row["session_id"] for row in result["sessions"]] == ["early-shared"]
    assert [row["stable_id"] for row in result["top_tracks"]] == ["track-b"]


def test_query_fails_without_creating_a_missing_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"

    with pytest.raises(AnalyticsSchemaError, match="does not exist"):
        query_play_analytics(missing)

    assert not missing.exists()


def test_query_fails_on_noncanonical_schema(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.db"
    with sqlite3.connect(malformed) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")

    with pytest.raises(AnalyticsSchemaError, match="required table"):
        query_play_analytics(malformed)


def test_query_supports_explicit_shared_state_event_table(analytics_db: Path) -> None:
    with sqlite3.connect(analytics_db) as connection:
        connection.execute("ALTER TABLE events RENAME TO set_events")

    result = query_play_analytics(analytics_db, events_table="set_events")

    assert result["summary"]["plays"] == 4


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "UPDATE sets SET started_at = 'not-a-date' WHERE ended_at IS NULL",
            "invalid sets.started_at timestamp",
        ),
        (
            "UPDATE events SET wall_clock = 'not-a-date' WHERE id = 1",
            "invalid events.wall_clock timestamp",
        ),
    ],
)
def test_query_rejects_malformed_timestamps(
    analytics_db: Path, sql: str, expected: str
) -> None:
    with sqlite3.connect(analytics_db) as connection:
        connection.execute(sql)

    with pytest.raises(AnalyticsSchemaError, match=expected):
        query_play_analytics(analytics_db)
