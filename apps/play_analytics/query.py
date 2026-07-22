"""Deterministic play analytics over the canonical set event store.

Requirements:
✔︎ Query the existing ``sets`` and ``set_events`` tables in SQLite read-only mode.
✔︎ Count only ``track_loaded`` events as plays and derive every view from one filter.
✔︎ Return stable JSON-ready ordering without inventing unavailable performance data.
✔︎ Fail explicitly for missing schema, malformed timestamps, or corrupt event JSON.

Acceptance tests:
[if] sessions and plays exist [then ⛔️] totals, history, or top tracks differ between repeated queries
[if] a share state is selected [then ⛔️] another state contributes to any projection
[if] the database contract is missing or corrupt [then ⛔️] an empty success payload is returned
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

ShareState = Literal["private", "shared_local", "shared_cloud"]
EventsTable = Literal["events", "set_events"]
_SHARE_STATES: frozenset[str] = frozenset({"private", "shared_local", "shared_cloud"})
_SESSION_COLUMNS = frozenset({"session_id", "started_at", "ended_at", "share_state"})
_EVENT_COLUMNS = frozenset(
    {"id", "session_id", "wall_clock", "track_stable_id", "action", "value_json"}
)


class AnalyticsSchemaError(RuntimeError):
    """The configured event store cannot satisfy the analytics contract."""


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    resolved = db_path.resolve()
    if not resolved.is_file():
        raise AnalyticsSchemaError(f"event store does not exist: {resolved}")
    try:
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error as exc:
        raise AnalyticsSchemaError(f"cannot open event store read-only: {exc}") from exc


def _require_schema(connection: sqlite3.Connection, events_table: EventsTable) -> None:
    for table, required_columns in (
        ("sets", _SESSION_COLUMNS),
        (events_table, _EVENT_COLUMNS),
    ):
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is None:
            raise AnalyticsSchemaError(f"required table {table!r} is missing")
        columns = {
            str(column["name"])
            for column in connection.execute(f"PRAGMA table_info({table})")
        }
        missing = sorted(required_columns - columns)
        if missing:
            raise AnalyticsSchemaError(
                f"required columns missing from {table!r}: {', '.join(missing)}"
            )


def _duration_seconds(started_at: str, ended_at: str | None) -> float | None:
    started = _parse_timestamp(started_at, "sets.started_at")
    if ended_at is None:
        return None
    duration = _parse_timestamp(ended_at, "sets.ended_at") - started
    if duration.total_seconds() < 0:
        raise AnalyticsSchemaError("session ended_at precedes started_at")
    return duration.total_seconds()


def _parse_timestamp(value: str, context: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise AnalyticsSchemaError(f"invalid {context} timestamp: {exc}") from exc


def _event_metadata(value_json: str | None) -> tuple[str | None, str | None]:
    if value_json is None:
        return None, None
    try:
        value = json.loads(value_json)
    except json.JSONDecodeError as exc:
        raise AnalyticsSchemaError(f"invalid set_events value_json: {exc}") from exc
    if not isinstance(value, dict):
        raise AnalyticsSchemaError("set_events value_json must decode to an object")
    title = value.get("title")
    artist = value.get("artist")
    if title is not None and not isinstance(title, str):
        raise AnalyticsSchemaError("set_events title must be a string or null")
    if artist is not None and not isinstance(artist, str):
        raise AnalyticsSchemaError("set_events artist must be a string or null")
    return title, artist


def query_play_analytics(
    db_path: Path,
    *,
    share_state: ShareState | None = None,
    limit: int = 50,
    events_table: EventsTable = "events",
) -> dict[str, Any]:
    """Return one deterministic, JSON-ready analytics read model.

    ``limit`` bounds both recent-session rows and top-track rows. Summary totals
    always cover the complete population selected by ``share_state``.
    """
    if share_state is not None and share_state not in _SHARE_STATES:
        raise ValueError(f"unsupported share_state: {share_state!r}")
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if events_table not in {"events", "set_events"}:
        raise ValueError(f"unsupported events_table: {events_table!r}")

    connection = _open_readonly(db_path)
    try:
        _require_schema(connection, events_table)
        where = "WHERE share_state = ?" if share_state is not None else ""
        parameters: tuple[str, ...] = (share_state,) if share_state is not None else ()
        session_rows = connection.execute(
            f"""
            SELECT session_id, started_at, ended_at, share_state
            FROM sets
            {where}
            ORDER BY started_at DESC, session_id DESC
            """,
            parameters,
        ).fetchall()
        session_rows.sort(key=lambda row: str(row["session_id"]), reverse=True)
        session_rows.sort(
            key=lambda row: _parse_timestamp(str(row["started_at"]), "sets.started_at"),
            reverse=True,
        )
        session_ids = [str(row["session_id"]) for row in session_rows]
        plays_by_session: Counter[str] = Counter()
        unique_by_session: dict[str, set[str]] = {
            session_id: set() for session_id in session_ids
        }
        track_counts: Counter[str] = Counter()
        track_latest: dict[str, tuple[str, str | None, str | None, int, datetime]] = {}

        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            play_rows = connection.execute(
                f"""
                SELECT id, session_id, wall_clock, track_stable_id, value_json
                FROM {events_table}
                WHERE action = 'track_loaded'
                  AND track_stable_id IS NOT NULL
                  AND session_id IN ({placeholders})
                ORDER BY wall_clock ASC, id ASC
                """,
                session_ids,
            ).fetchall()
            for row in play_rows:
                session_id = str(row["session_id"])
                stable_id = str(row["track_stable_id"])
                wall_clock = str(row["wall_clock"])
                played_at = _parse_timestamp(wall_clock, f"{events_table}.wall_clock")
                title, artist = _event_metadata(row["value_json"])
                plays_by_session[session_id] += 1
                unique_by_session[session_id].add(stable_id)
                track_counts[stable_id] += 1
                candidate = (wall_clock, title, artist, int(row["id"]), played_at)
                current = track_latest.get(stable_id)
                if current is None or (played_at, candidate[3]) > (
                    current[4],
                    current[3],
                ):
                    track_latest[stable_id] = candidate

        sessions: list[dict[str, Any]] = []
        completed_duration_s = 0.0
        for row in session_rows:
            session_id = str(row["session_id"])
            duration_s = _duration_seconds(str(row["started_at"]), row["ended_at"])
            if duration_s is not None:
                completed_duration_s += duration_s
            sessions.append(
                {
                    "session_id": session_id,
                    "started_at": str(row["started_at"]),
                    "ended_at": row["ended_at"],
                    "duration_s": duration_s,
                    "share_state": str(row["share_state"]),
                    "play_count": plays_by_session[session_id],
                    "unique_track_count": len(unique_by_session[session_id]),
                }
            )

        top_track_ids = sorted(track_counts)
        top_track_ids.sort(
            key=lambda stable_id: (
                track_latest[stable_id][0],
                track_latest[stable_id][3],
            ),
            reverse=True,
        )
        top_track_ids.sort(
            key=lambda stable_id: track_counts[stable_id],
            reverse=True,
        )
        top_tracks = [
            {
                "stable_id": stable_id,
                "title": track_latest[stable_id][1],
                "artist": track_latest[stable_id][2],
                "play_count": track_counts[stable_id],
                "last_played_at": track_latest[stable_id][0],
            }
            for stable_id in top_track_ids[:limit]
        ]
        return {
            "schema_version": 1,
            "filters": {"share_state": share_state, "limit": limit},
            "summary": {
                "sessions": len(session_rows),
                "plays": sum(track_counts.values()),
                "unique_tracks": len(track_counts),
                "completed_duration_s": completed_duration_s,
            },
            "sessions": sessions[:limit],
            "top_tracks": top_tracks,
        }
    except sqlite3.Error as exc:
        raise AnalyticsSchemaError(f"event-store query failed: {exc}") from exc
    finally:
        connection.close()


__all__ = [
    "AnalyticsSchemaError",
    "EventsTable",
    "ShareState",
    "query_play_analytics",
]
