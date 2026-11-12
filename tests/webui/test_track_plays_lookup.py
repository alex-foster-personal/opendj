"""POST /tracks/plays:lookup returns Open DJ play counts for shuffle (#11325).

[if] lookup asks for stable ids [then] opendj_play_count matches Open DJ rows only, [else stop].
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import play_log
from apps.webui.server.routes.track_plays import router
from apps.webui.server.sqlite_backend import make_backend

pytestmark = pytest.mark.requirement("LIBM-12643")

A = "a" * 40
B = "b" * 40


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    state = tmp_path / "state.db"
    conn = state_db.open_rw(state)
    try:
        for sid in (A, B):
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
                "VALUES (?, 'inferred', ?, '2026-01-01', '2026-01-01')",
                (sid, sid[0]),
            )
        conn.commit()
    finally:
        conn.close()
    app = FastAPI()
    app.state.backend = make_backend()
    app.state.state_db_path = state
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


def _record_opendj(conn_path: Path, sid: str, play_id: str) -> None:
    conn = state_db.open_rw(conn_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        play_log.record_play(
            conn,
            sid,
            play_id=play_id,
            audible_s=75.0,
            deck=1,
            duration_ms=200_000,
            ts="2026-01-01T00:00:00Z",
            actor="test",
        )
        conn.commit()
    finally:
        conn.close()


def test_lookup_counts_opendj_plays_only(client: TestClient, tmp_path: Path) -> None:
    """[if] two tracks differ in Open DJ play_count [then] lookup reports each count, [else stop]."""
    state = tmp_path / "state.db"
    _record_opendj(state, A, "play-1")
    _record_opendj(state, A, "play-2")
    r = client.post("/api/v1/tracks/plays:lookup", json={"stable_ids": [A, B]})
    assert r.status_code == 200, r.text
    rows = {row["stable_id"]: row["opendj_play_count"] for row in r.json()["rows"]}
    assert rows[A] == 2
    assert rows[B] == 0
