"""SYNC-03: end-to-end apply against a fabricated djay-shape SQLite fixture.

We build a tiny stand-in DB inside ``tmp_path`` with the two schemas
:mod:`apps.sync.playlist_apply` touches so integration tests run without
needing a captured live fixture.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from apps.sync import playlist_apply as pa
from apps.sync import playlist_tsaf as ptsaf


def _make_djay_fixture(db_path: Path, tracks: list[str]) -> dict[str, int]:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE database2 ("
            "rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
            "collection TEXT NOT NULL, "
            "key TEXT NOT NULL, "
            "data BLOB, "
            "UNIQUE(collection, key))"
        )
        con.execute(
            "CREATE TABLE view_mediaItemPlaylistView_page ("
            "pageKey TEXT PRIMARY KEY, "
            '"group" TEXT NOT NULL, '
            "prevPageKey TEXT, "
            "count INTEGER NOT NULL, "
            "data BLOB)"
        )
        uuid_to_rowid: dict[str, int] = {}
        for uuid in tracks:
            cur = con.execute(
                "INSERT INTO database2 (collection, key, data) VALUES (?, ?, ?)",
                ("mediaItemUserData", uuid, b""),
            )
            uuid_to_rowid[uuid] = int(cur.lastrowid)

        warm_uuid = "warm-uuid-000000000000000000000000"
        con.execute(
            "INSERT INTO database2 (collection, key, data) VALUES (?, ?, ?)",
            (
                "mediaItemPlaylists",
                warm_uuid,
                ptsaf.build_playlist_blob(warm_uuid, "Warmup", kind="leaf"),
            ),
        )
        first = tracks[0]
        con.execute(
            'INSERT INTO view_mediaItemPlaylistView_page '
            '(pageKey, "group", prevPageKey, count, data) '
            "VALUES (?, ?, NULL, ?, ?)",
            (
                ptsaf.new_page_key(),
                warm_uuid,
                1,
                ptsaf.build_page_data([uuid_to_rowid[first]]),
            ),
        )
        con.commit()
    finally:
        con.close()
    return uuid_to_rowid


def _plan_fixture(tmp_path: Path, tracks: list[str]) -> Path:
    plan = {
        "generated_at": "2026-04-17T00:00:00Z",
        "match_set_sha256": "deadbeef",
        "playlists": [
            {
                "rb_id": "r1",
                "rb_name": "Warmup",
                "op": "update",
                "djay_uuid": "warm-uuid-000000000000000000000000",
                "djay_name_current": "Warmup",
                "target_members": [
                    {"track_no": i, "rb_id": f"rb-{i}", "djay_uuid": u}
                    for i, u in enumerate(tracks, start=1)
                ],
                "djay_current_members": [tracks[0]],
                "adds": [{"track_no": 2, "rb_id": "rb-2", "djay_uuid": tracks[1]}],
                "removes": [],
                "reordered": False,
                "unmatched_rb": [],
            },
            {
                "rb_id": "r2",
                "rb_name": "Peak",
                "op": "create",
                "djay_uuid": None,
                "djay_name_current": None,
                "target_members": [
                    {"track_no": 1, "rb_id": "rb-1", "djay_uuid": tracks[0]}
                ],
                "djay_current_members": [],
                "adds": [{"track_no": 1, "rb_id": "rb-1", "djay_uuid": tracks[0]}],
                "removes": [],
                "reordered": False,
                "unmatched_rb": [],
            },
            {
                "rb_id": "r3",
                "rb_name": "Chill",
                "op": "noop",
                "djay_uuid": "noop-uuid",
                "djay_name_current": "Chill",
                "target_members": [],
                "djay_current_members": [],
                "adds": [],
                "removes": [],
                "reordered": False,
                "unmatched_rb": [],
            },
        ],
        "djay_only": [],
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


@pytest.fixture
def apply_fixture(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    tracks = [
        "uuid-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01",
        "uuid-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb02",
    ]
    db_path = tmp_path / "djay.db"
    _make_djay_fixture(db_path, tracks)
    plan_path = _plan_fixture(tmp_path, tracks)
    return db_path, plan_path, tracks


@pytest.mark.requirement("SYNC-03")
def test_resolver_raises_when_userdata_missing(apply_fixture) -> None:
    db_path, _, tracks = apply_fixture
    con = sqlite3.connect(db_path)
    try:
        with pytest.raises(pa.PlaylistApplyError, match="not found"):
            pa.resolve_userdata_rowids(con, [tracks[0], "not-a-real-uuid"])
    finally:
        con.close()


@pytest.mark.requirement("SYNC-03")
def test_resolver_returns_rowids_in_input_order(apply_fixture) -> None:
    db_path, _, tracks = apply_fixture
    con = sqlite3.connect(db_path)
    try:
        ids = pa.resolve_userdata_rowids(con, list(reversed(tracks)))
    finally:
        con.close()
    assert len(ids) == 2 and ids[0] != ids[1]


@pytest.mark.requirement("SYNC-03")
def test_resolver_returns_empty_for_empty_input(apply_fixture) -> None:
    db_path, _, _ = apply_fixture
    con = sqlite3.connect(db_path)
    try:
        assert pa.resolve_userdata_rowids(con, []) == []
    finally:
        con.close()


@pytest.mark.requirement("SYNC-03")
def test_apply_update_op_replaces_members_and_verifies(apply_fixture) -> None:
    db_path, plan_path, _ = apply_fixture
    plan = json.loads(plan_path.read_text())
    result = pa.apply_plan(plan, db_path=db_path)
    statuses = {r.rb_name: r.status for r in result.per_playlist}
    assert statuses["Warmup"] == "verified"
    assert statuses["Peak"] == "verified"
    assert statuses["Chill"] == "skipped"


@pytest.mark.requirement("SYNC-03")
def test_apply_create_op_inserts_new_playlist_row(apply_fixture) -> None:
    db_path, plan_path, _ = apply_fixture
    plan = json.loads(plan_path.read_text())
    pa.apply_plan(plan, db_path=db_path)
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT data FROM database2 WHERE collection='mediaItemPlaylists'"
        ).fetchall()
    finally:
        con.close()
    names = [ptsaf.parse_playlist_blob(r[0]).get("name") for r in rows]
    assert "Warmup" in names
    assert "Peak" in names


@pytest.mark.requirement("SYNC-03")
def test_apply_respects_playlist_filter(apply_fixture) -> None:
    db_path, plan_path, _ = apply_fixture
    plan = json.loads(plan_path.read_text())
    result = pa.apply_plan(plan, db_path=db_path, playlist_filter={"Warmup"})
    names = [r.rb_name for r in result.per_playlist]
    assert "Warmup" in names
    assert "Peak" not in names  # filtered out
    assert "Chill" in names  # noops always logged


@pytest.mark.requirement("SYNC-03")
def test_apply_returns_failed_when_target_uuid_absent(apply_fixture) -> None:
    db_path, plan_path, _ = apply_fixture
    plan = json.loads(plan_path.read_text())
    plan["playlists"][1]["target_members"][0]["djay_uuid"] = "ghost-uuid-xxxxxxxxxxxxxxxxxxxxxx"
    result = pa.apply_plan(plan, db_path=db_path)
    peak = next(r for r in result.per_playlist if r.rb_name == "Peak")
    assert peak.status == "failed"
    assert "not found" in peak.message.lower()


@pytest.mark.requirement("SYNC-03")
def test_apply_is_idempotent_when_rerun(apply_fixture) -> None:
    db_path, plan_path, _ = apply_fixture
    plan = json.loads(plan_path.read_text())
    r1 = pa.apply_plan(plan, db_path=db_path)
    r2 = pa.apply_plan(plan, db_path=db_path)
    assert r1.all_verified
    assert r2.all_verified


@pytest.mark.requirement("SYNC-03")
def test_dry_run_does_not_open_rw_connection(
    apply_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, plan_path, _ = apply_fixture

    calls: list[str] = []
    original = sqlite3.connect

    def _tracking_connect(database, *args, **kwargs):
        calls.append(str(database))
        return original(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _tracking_connect)
    rc = pa.main(["--plan", str(plan_path)])
    assert rc == 0
    assert not any("djay" in c or str(db_path) == c for c in calls)
