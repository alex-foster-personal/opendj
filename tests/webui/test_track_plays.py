"""Open DJ's own play log (PLAYS-01): library play count = rekordbox + Open DJ.

[if] a deck load is heard for the play threshold [then] the library play count is rekordbox plays plus Open DJ plays, [else stop].

Regression one-liners:
  - if a POSTed play is not logged then Open DJ plays never count
  - if a retried play_id counts twice then one play inflates the count
  - if a play under the threshold is accepted then auditions inflate the count
  - if the library row's play_count drops either rekordbox's DJPlayCount or the
    own plays then the combined number is wrong
  - if a track with no rekordbox mapping cannot accumulate plays then broken
  - if the library wheel's play_count axis ignores own plays then it disagrees
    with the track list
  - if an unknown or removed track accepts a play then orphan rows appear
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.library_wheel.query import query_library_wheel
from apps.shared.state import db as state_db
from apps.shared.state import play_log
from apps.webui.server.backend import Track
from apps.webui.server.rb_vendor_pkg import track_rows
from apps.webui.server.routes.track_plays import router
from apps.webui.server.sqlite_backend import make_backend

pytestmark = [pytest.mark.requirement("PLAYS-01")]

MAPPED = "a" * 40
LOCAL = "c" * 40
REMOVED = "d" * 40
VENDOR_ID = "126790091"
RB_PLAYS = 7


def _master(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE djmdContent (ID VARCHAR(255) PRIMARY KEY, "
            "FolderPath VARCHAR(255), ImagePath VARCHAR(255), "
            "AnalysisDataPath VARCHAR(255), Commnt VARCHAR(255), GenreID VARCHAR(255), "
            "DJPlayCount INTEGER, rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE djmdGenre (ID VARCHAR(255) PRIMARY KEY, "
            "Name VARCHAR(255), rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        conn.execute("INSERT INTO djmdGenre (ID, Name) VALUES ('g1', 'House')")
        conn.execute(
            "INSERT INTO djmdContent (ID, GenreID, DJPlayCount) VALUES (?, 'g1', ?)",
            (VENDOR_ID, RB_PLAYS),
        )
        conn.commit()
    finally:
        conn.close()


def _state(path: Path) -> None:
    conn = state_db.open_rw(path)
    try:
        for sid in (MAPPED, LOCAL, REMOVED):
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, "
                "created_at, updated_at) VALUES (?, 'inferred', ?, '2026-01-01', '2026-01-01')",
                (sid, f"track {sid[0]}"),
            )
        conn.execute(
            "UPDATE tracks SET deleted_at = '2026-02-01' WHERE stable_id = ?", (REMOVED,)
        )
        conn.execute(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
            "VALUES (?, 'rekordbox', ?)",
            (MAPPED, VENDOR_ID),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    master, state = tmp_path / "master.plain.db", tmp_path / "state.db"
    _master(master)
    _state(state)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master)
    monkeypatch.setattr(rb_config, "STATE_DB", state)
    return SimpleNamespace(master=master, state=state)


@pytest.fixture
def client(paths: SimpleNamespace) -> Iterator[TestClient]:
    app = FastAPI()
    app.state.backend = make_backend()
    app.state.state_db_path = paths.state
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


def _play(client: TestClient, sid: str, play_id: str, audible_s: float = 75.0):
    return client.post(
        f"/api/v1/tracks/{sid}/plays",
        json={"play_id": play_id, "audible_s": audible_s, "deck": 1, "duration_ms": 200_000},
    )


def test_play_adds_to_rekordbox_count_and_retry_is_idempotent(client: TestClient) -> None:
    before = client.get(f"/api/v1/tracks/{MAPPED}/plays").json()
    assert before["play_count"] == RB_PLAYS
    assert before["opendj_play_count"] == 0

    first = _play(client, MAPPED, "load-0001")
    assert first.status_code == 200, first.text
    assert first.json()["recorded"] is True
    assert first.json()["play_count"] == RB_PLAYS + 1

    retry = _play(client, MAPPED, "load-0001")
    assert retry.json()["recorded"] is False
    assert retry.json()["play_count"] == RB_PLAYS + 1, "a retried play_id counted twice"

    _play(client, MAPPED, "load-0002")
    body = client.get(f"/api/v1/tracks/{MAPPED}/plays").json()
    assert body == {
        "stable_id": MAPPED,
        "play_count": RB_PLAYS + 2,
        "rekordbox_play_count": RB_PLAYS,
        "opendj_play_count": 2,
        "opendj_last_played_at": body["opendj_last_played_at"],
        "recorded": None,
    }
    assert body["opendj_last_played_at"] is not None


def test_unmapped_track_counts_own_plays(client: TestClient) -> None:
    _play(client, LOCAL, "load-local-1")
    body = client.get(f"/api/v1/tracks/{LOCAL}/plays").json()
    assert (body["rekordbox_play_count"], body["opendj_play_count"]) == (0, 1)


def test_under_threshold_play_is_refused(client: TestClient, paths: SimpleNamespace) -> None:
    resp = _play(client, MAPPED, "load-short", audible_s=play_log.PLAY_THRESHOLD_S - 1)
    assert resp.status_code == 422
    # Control: the threshold itself is accepted, so the refusal above is the
    # threshold and not a broken body.
    assert _play(client, MAPPED, "load-exact", audible_s=play_log.PLAY_THRESHOLD_S).status_code == 200


@pytest.mark.parametrize("sid", [REMOVED, "f" * 40])
def test_unknown_or_removed_track_is_404(client: TestClient, paths: SimpleNamespace, sid: str) -> None:
    assert _play(client, sid, "load-orphan").status_code == 404
    conn = sqlite3.connect(str(paths.state))
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ?", (play_log.PLAY_KIND,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows == 0


def _wheel_counts(node: object, out: dict[str, int]) -> dict[str, int]:
    if isinstance(node, dict):
        if "stable_id" in node and "axis_value" in node:
            out[node["stable_id"]] = node["axis_value"]
        for value in node.values():
            _wheel_counts(value, out)
    elif isinstance(node, list):
        for value in node:
            _wheel_counts(value, out)
    return out


def test_library_wheel_play_count_adds_own_plays(
    client: TestClient, paths: SimpleNamespace
) -> None:
    before = _wheel_counts(query_library_wheel(paths.state, paths.master, axis="play_count"), {})
    assert before.get(MAPPED) == RB_PLAYS, before  # control: the walk finds the row
    _play(client, MAPPED, "load-wheel-1")
    after = _wheel_counts(query_library_wheel(paths.state, paths.master, axis="play_count"), {})
    assert after.get(MAPPED) == RB_PLAYS + 1


def test_track_rows_play_count_adds_own_plays(client: TestClient) -> None:
    _play(client, MAPPED, "load-tr-1")
    _play(client, LOCAL, "load-tr-2")
    _play(client, LOCAL, "load-tr-3")
    rows = {
        row["stable_id"]: row
        for row in track_rows.build_track_rows(
            [Track(stable_id=MAPPED), Track(stable_id=LOCAL)]
        )
    }
    assert rows[MAPPED]["play_count"] == RB_PLAYS + 1
    assert rows[LOCAL]["play_count"] == 2
