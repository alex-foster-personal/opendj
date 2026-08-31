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
    assert result["filters"] == {
        "share_state": None,
        "limit": 50,
        "min_audible_s": 60.0,
    }
    # These fixture rows carry no audible_s (they stand in for rb_history /
    # djay rows), so the read-time dwell filter must not touch them.
    assert result["summary"] == {
        "sessions": 2,
        "plays": 4,
        "unique_tracks": 3,
        "completed_duration_s": 5400.0,
        "plays_below_min_audible": 0,
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

    assert result["filters"] == {
        "share_state": "shared_local",
        "limit": 1,
        "min_audible_s": 60.0,
    }
    assert result["summary"] == {
        "sessions": 1,
        "plays": 2,
        "unique_tracks": 2,
        "completed_duration_s": 5400.0,
        "plays_below_min_audible": 0,
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


# ---------------------------------------------------------------------------
# read-time dwell filter
# ---------------------------------------------------------------------------


def _add_opendj_plays(db_path: Path) -> None:
    """Append Open DJ own-deck plays carrying real dwell values.

    12s is an audition, 240s is a full play. Both are RECORDED; only the
    read-time filter separates them.
    """
    connection = sqlite3.connect(db_path)
    try:
        connection.executemany(
            """
            INSERT INTO events(
                session_id, timestamp_s, wall_clock, deck, track_stable_id,
                action, value_json, source
            ) VALUES (?, ?, ?, ?, ?, 'track_loaded', ?, 'opendj_decks')
            """,
            [
                (
                    "late-private",
                    100.0,
                    "2026-07-21T23:00:00+00:00",
                    "1",
                    "odj-audition",
                    '{"title":"Audition","artist":"A","audible_s":12.0,'
                    '"played_fraction":0.05,"threshold_s":60.0}',
                ),
                (
                    "late-private",
                    200.0,
                    "2026-07-21T23:10:00+00:00",
                    "2",
                    "odj-full",
                    '{"title":"Full","artist":"B","audible_s":240.0,'
                    '"played_fraction":1.0,"threshold_s":60.0}',
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def test_default_filter_drops_the_audition_but_keeps_the_full_play(
    analytics_db: Path,
) -> None:
    _add_opendj_plays(analytics_db)
    result = query_play_analytics(analytics_db, limit=50)

    ids = {row["stable_id"] for row in result["top_tracks"]}
    assert "odj-full" in ids
    assert "odj-audition" not in ids
    assert result["summary"]["plays_below_min_audible"] == 1


def test_lowering_the_filter_recovers_the_audition_from_the_same_rows(
    analytics_db: Path,
) -> None:
    """The whole point of filtering at read time: nothing was destroyed."""
    _add_opendj_plays(analytics_db)
    result = query_play_analytics(analytics_db, limit=50, min_audible_s=0.0)

    ids = {row["stable_id"] for row in result["top_tracks"]}
    assert {"odj-audition", "odj-full"} <= ids
    assert result["summary"]["plays_below_min_audible"] == 0
    assert result["filters"]["min_audible_s"] == 0.0


def test_raising_the_filter_drops_both_open_dj_plays(analytics_db: Path) -> None:
    _add_opendj_plays(analytics_db)
    result = query_play_analytics(analytics_db, limit=50, min_audible_s=300.0)

    ids = {row["stable_id"] for row in result["top_tracks"]}
    assert "odj-full" not in ids
    assert result["summary"]["plays_below_min_audible"] == 2


def test_rows_without_dwell_are_never_filtered(analytics_db: Path) -> None:
    """rb_history / djay rows carry no audible_s; their upstream already judged."""
    _add_opendj_plays(analytics_db)
    strict = query_play_analytics(analytics_db, limit=50, min_audible_s=9999.0)
    # The 4 dwell-less fixture plays survive an absurd threshold.
    assert strict["summary"]["plays"] == 4
    assert strict["summary"]["plays_below_min_audible"] == 2


def test_negative_filter_is_rejected(analytics_db: Path) -> None:
    with pytest.raises(ValueError, match="min_audible_s"):
        query_play_analytics(analytics_db, min_audible_s=-1.0)


def test_non_numeric_audible_s_fails_explicitly(analytics_db: Path) -> None:
    connection = sqlite3.connect(analytics_db)
    try:
        connection.execute(
            """
            INSERT INTO events(
                session_id, timestamp_s, wall_clock, deck, track_stable_id,
                action, value_json, source
            ) VALUES ('late-private', 300.0, '2026-07-21T23:20:00+00:00', '1',
                      'odj-bad', 'track_loaded', '{"audible_s":"lots"}',
                      'opendj_decks')
            """
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(AnalyticsSchemaError, match="audible_s"):
        query_play_analytics(analytics_db, limit=50)
