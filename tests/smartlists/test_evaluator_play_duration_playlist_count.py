"""Smartlist rules: play_count, duration_s, playlist_count (issue #10023).

[if] SMART-10718 rules are marked [then] module docstring names the three fields, [else stop].
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.analysis.selection import Selection
from apps.shared.state import db as state_db
from apps.shared.state.play_log import record_play
from apps.smartlists.evaluator import evaluate
from apps.smartlists.materializer import DEFAULT_MARKER_PREFIX
from apps.smartlists.repo import SmartlistsRepo
from apps.webui.server.app import create_app
from apps.webui.server.routes import smartlists as smartlists_routes
from apps.webui.server.sqlite_backend import SqliteBackend
from fastapi.testclient import TestClient

pytestmark = pytest.mark.requirement("SMART-10718")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _insert_track(conn: sqlite3.Connection, stable_id: str, *, duration_ms: int | None = None) -> None:
    ts = _now()
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, created_at, updated_at) "
        "VALUES (?, 'isrc', ?, ?, ?, ?)",
        (stable_id, stable_id, duration_ms, ts, ts),
    )


def _record_opendj_plays(conn: sqlite3.Connection, stable_id: str, count: int) -> None:
    for i in range(count):
        record_play(
            conn,
            stable_id,
            play_id=f"{stable_id}-play-{i}",
            audible_s=90.0,
            deck=1,
            duration_ms=240_000,
            ts=_now(),
            actor="test",
        )


@pytest.mark.requirement("SMART-10718")
def test_play_count_zero_selects_tracks_without_play_log(state_conn) -> None:
    """[if] play_count = 0 [then] only tracks with no play-log rows match, [else stop]."""
    _insert_track(state_conn, "never")
    _insert_track(state_conn, "played")
    _record_opendj_plays(state_conn, "played", 1)
    result = evaluate({"field": "play_count", "op": "=", "value": 0}, state_conn)
    assert result == ["never"]


@pytest.mark.requirement("SMART-10718")
def test_play_count_uses_play_log_not_rekordbox_field(state_conn) -> None:
    """[if] play_count >= 3 [then] only play-log rows count, not rekordbox play_count, [else stop]."""
    _insert_track(state_conn, "rb-only")
    _insert_track(state_conn, "log-three")
    state_conn.execute(
        "INSERT INTO track_fields (stable_id, field_name, value_json, source, modified_at) "
        "VALUES (?, 'play_count', ?, 'rekordbox', ?)",
        ("rb-only", json.dumps(99), _now()),
    )
    _record_opendj_plays(state_conn, "log-three", 3)
    result = evaluate({"field": "play_count", "op": ">=", "value": 3}, state_conn)
    assert result == ["log-three"]


@pytest.mark.requirement("SMART-10718")
def test_duration_s_between_and_missing(state_conn) -> None:
    """[if] duration_s between/missing [then] seconds from rounded duration_ms, null only on missing, [else stop]."""
    _insert_track(state_conn, "short", duration_ms=179_400)
    _insert_track(state_conn, "mid", duration_ms=240_000)
    _insert_track(state_conn, "long", duration_ms=301_000)
    _insert_track(state_conn, "unknown", duration_ms=None)
    between = evaluate({"field": "duration_s", "op": "between", "value": [180, 300]}, state_conn)
    assert set(between) == {"mid"}
    missing = evaluate({"field": "duration_s", "op": "missing", "value": None}, state_conn)
    assert missing == ["unknown"]


@pytest.mark.requirement("SMART-10718")
def test_playlist_count_zero_ignores_smartlist_playlists(state_conn) -> None:
    """[if] playlist_count = 0 [then] non-deleted user playlists count; [SL] playlists do not, [else stop]."""
    _insert_track(state_conn, "orphan")
    _insert_track(state_conn, "user-pl")
    _insert_track(state_conn, "sl-only")
    now = _now()
    state_conn.execute(
        "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        ("pl-user", "My crate", "manual", "pl-user", now, now),
    )
    state_conn.execute(
        "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (
            "pl-sl",
            f"{DEFAULT_MARKER_PREFIX}Auto",
            "manual",
            "pl-sl",
            now,
            now,
        ),
    )
    state_conn.executemany(
        "INSERT INTO playlist_memberships (playlist_id, stable_id, position) VALUES (?,?,?)",
        [
            ("pl-user", "user-pl", 0),
            ("pl-sl", "sl-only", 0),
        ],
    )
    result = evaluate({"field": "playlist_count", "op": "=", "value": 0}, state_conn)
    assert set(result) == {"orphan", "sl-only"}


@pytest.mark.requirement("SMART-10718")
def test_evaluate_new_fields_uses_set_based_sql_not_per_track(state_conn) -> None:
    """[if] 10k tracks use play_count,duration_s,playlist_count [then] one query, not per-track, [else stop]."""
    conn = state_conn
    conn.execute("BEGIN")
    rows = [(f"t{i:05d}", 200_000 + (i % 120) * 1000) for i in range(10_000)]
    ts = _now()
    conn.executemany(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, created_at, updated_at) "
        "VALUES (?, 'isrc', ?, ?, ?, ?)",
        [(sid, sid, dur, ts, ts) for sid, dur in rows],
    )
    conn.commit()
    selection = Selection.resolve(conn)
    statements: list[str] = []
    conn.set_trace_callback(lambda sql: statements.append(sql))
    rule = {
        "op": "and",
        "children": [
            {"field": "play_count", "op": "=", "value": 0},
            {"field": "duration_s", "op": ">=", "value": 200},
            {"field": "playlist_count", "op": "=", "value": 0},
        ],
    }
    matches = evaluate(rule, conn, selection=selection)
    assert len(matches) == 10_000
    track_selects = [s for s in statements if "FROM tracks" in s and s.lstrip().upper().startswith("SELECT")]
    assert len(track_selects) == 1


@pytest.mark.requirement("SMART-10718")
def test_http_smartlist_tracks_evaluates_play_count_rule(tmp_path: Path) -> None:
    """[if] GET /smartlists/{id}/tracks [then] new numeric fields evaluate without the UI, [else stop]."""
    db_path = tmp_path / "state.db"
    conn = state_db.open_rw(db_path, apply_schema=True)
    try:
        ensure_smartlists = __import__(
            "apps.shared.pairings.schema_sql", fromlist=["ensure_phase08_tables"]
        ).ensure_phase08_tables
        ensure_smartlists(conn)
        _insert_track(conn, "quiet")
        _insert_track(conn, "busy")
        _record_opendj_plays(conn, "busy", 2)
        rule = {"field": "play_count", "op": ">=", "value": 2}
        sid = SmartlistsRepo(conn).create("Busy tracks", rule).id
        conn.commit()
    finally:
        conn.close()
    app = create_app(
        backend=SqliteBackend(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(db_path),
        mount_frontend=False,
    )
    app.include_router(smartlists_routes.router, prefix="/api/v1")
    client = TestClient(app, base_url="http://test-host")
    r = client.get(f"/api/v1/smartlists/{sid}/tracks")
    assert r.status_code == 200, r.text
    assert [row["stable_id"] for row in r.json()["tracks"]] == ["busy"]
