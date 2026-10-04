"""Open DJ's own cue store (CUES-01): cues for every track, rekordbox's copied in.

[if] a track has no rekordbox mapping, or rekordbox cues were imported [then] hot cues save, serve and survive re-import from the own store, [else stop].

Regression one-liners:
  - if a track with no rekordbox mapping cannot SAVE a hot cue then broken
  - if editing one slot of a rekordbox track drops its other rekordbox cues then broken
  - if an edit writes rekordbox's djmdCue then broken
  - if /anlz serves rekordbox cues for a track that has its own set then broken
  - if ingest overwrites cues the user edited in Open DJ then broken
  - if ingest stops refreshing cues nobody edited (the overshoot) then broken
  - if Kind 9-11 or deleted djmdCue rows are imported then broken
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import cue_store
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import rekordbox as rb_ingest
from apps.shared.state.writer import StateWriter
from apps.webui.server import rb_vendor
from apps.webui.server.rb_vendor_pkg.own_cues_overlay import apply_own_cues
from apps.webui.server.routes.rb_hot_cues import router
from apps.webui.server.sqlite_backend import make_backend

pytestmark = [pytest.mark.requirement("CUES-01")]

MAPPED = "a" * 40
LOCAL = "c" * 40
VENDOR_ID = "126790091"
MODIFIED_AT = "2026-10-01T10:00:00+00:00"


def _master(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE djmdContent (ID VARCHAR(255) PRIMARY KEY, "
            "FolderPath VARCHAR(255), ImagePath VARCHAR(255), "
            "AnalysisDataPath VARCHAR(255), Length INTEGER, "
            "Commnt VARCHAR(255), GenreID VARCHAR(255), "
            "rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE djmdGenre (ID VARCHAR(255) PRIMARY KEY, "
            "Name VARCHAR(255), rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE djmdCue ("
            "ID VARCHAR(255) PRIMARY KEY, ContentID VARCHAR(255), "
            "InMsec INTEGER, InFrame INTEGER, InMpegFrame INTEGER, "
            "InMpegAbs INTEGER, OutMsec INTEGER, OutFrame INTEGER, "
            "Kind INTEGER, Color INTEGER, ColorTableIndex INTEGER, "
            "ActiveLoop INTEGER, Comment VARCHAR(255), BeatLoopSize INTEGER, "
            "rb_local_deleted TINYINT(1) DEFAULT 0, "
            "created_at DATETIME, updated_at DATETIME)"
        )
        conn.execute("INSERT INTO djmdContent (ID, Length) VALUES (?, 300)", (VENDOR_ID,))
        # Rekordbox already holds hot cue A and one memory cue for this track.
        conn.execute(
            "INSERT INTO djmdCue (ID, ContentID, InMsec, Kind, ColorTableIndex, "
            "Comment, ActiveLoop) VALUES ('1', ?, 1000, 1, 3, 'intro', 0)",
            (VENDOR_ID,),
        )
        conn.execute(
            "INSERT INTO djmdCue (ID, ContentID, InMsec, Kind, ActiveLoop) "
            "VALUES ('2', ?, 64000, 0, 0)",
            (VENDOR_ID,),
        )
        conn.commit()
    finally:
        conn.close()


def _state(path: Path) -> None:
    conn = state_db.open_rw(path)
    try:
        for sid, duration in ((MAPPED, None), (LOCAL, 240_000)):
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, "
                "created_at, updated_at) VALUES (?, 'inferred', ?, '2026-01-01', '2026-01-01')",
                (sid, duration),
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


def _slots(client: TestClient, sid: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/tracks/{sid}/hot-cues")
    assert response.status_code == 200, response.text
    return {row["slot"]: row for row in response.json()}


def _save(client: TestClient, sid: str, slot: str, in_ms: int, **body: Any):
    return client.put(
        f"/api/v1/tracks/{sid}/hot-cues/{slot}",
        json={"in_ms": in_ms, **body},
        headers={"If-Match": _slots(client, sid)[slot]["revision"]},
    )


def _row(state: Path, sid: str) -> tuple[dict[str, Any], str] | None:
    conn = state_db.open_ro(state)
    try:
        return cue_store.read_row(conn, sid)
    finally:
        conn.close()


def _stored(state: Path, sid: str) -> tuple[dict[str, Any], str]:
    row = _row(state, sid)
    assert row is not None, f"no cue_points row for {sid}"
    return row


# ----- the headline: a track rekordbox never saw keeps cues ------------------


def test_local_track_saves_and_reads_back_a_hot_cue(client: TestClient, paths) -> None:
    assert all(row["cue"] is None for row in _slots(client, LOCAL).values())
    saved = _save(client, LOCAL, "C", 30_000, comment="drop")
    assert saved.status_code == 200, saved.text
    cue = _slots(client, LOCAL)["C"]["cue"]
    assert (cue["in_ms"], cue["comment"]) == (30_000, "drop")
    blob, source = _stored(paths.state, LOCAL)
    assert source == "webui"
    assert [c["slot"] for c in blob["cues"]] == ["C"]


def test_local_track_position_is_bounded_by_its_own_duration(client: TestClient) -> None:
    beyond = _save(client, LOCAL, "A", 240_001)
    assert beyond.status_code == 422
    assert beyond.json()["detail"]["code"] == "INVALID_CUE_POSITION"


def test_a_zero_stored_duration_is_unknown_not_a_zero_length_bound(
    client: TestClient, paths
) -> None:
    conn = state_db.open_rw(paths.state)
    try:
        conn.execute("UPDATE tracks SET duration_ms = 0 WHERE stable_id = ?", (LOCAL,))
        conn.commit()
    finally:
        conn.close()
    saved = _save(client, LOCAL, "A", 30_000)
    assert saved.status_code == 200, saved.text


def test_a_mapped_track_keeps_own_cues_without_a_rekordbox_database(
    client: TestClient, paths
) -> None:
    paths.master.unlink()
    assert all(row["cue"] is None for row in _slots(client, MAPPED).values())
    saved = _save(client, MAPPED, "B", 5_000)
    assert saved.status_code == 200, saved.text
    assert _slots(client, MAPPED)["B"]["cue"]["in_ms"] == 5_000
    # Control: an unknown track is still a 404, not an empty cue set.
    assert client.get("/api/v1/tracks/no-such-track/hot-cues").status_code == 404


def test_an_unusable_vendor_cue_is_dropped_not_a_500(paths) -> None:
    vendor = [
        {"kind": "hot_cue", "slot": "A", "in_ms": 1_000, "out_ms": None},
        {"kind": "hot_cue", "slot": "B", "in_ms": None, "out_ms": None},
    ]
    conn = state_db.open_ro(paths.state)
    try:
        loaded = cue_store.load(conn, MAPPED, lambda: vendor)
    finally:
        conn.close()
    assert [(c["slot"], c["in_ms"]) for c in loaded.cues] == [("A", 1_000)]


# ----- rekordbox cues are the default, and survive an edit ------------------


def test_mapped_track_without_own_row_shows_rekordbox_cues(client: TestClient, paths) -> None:
    cue = _slots(client, MAPPED)["A"]["cue"]
    assert (cue["in_ms"], cue["comment"], cue["color_table_index"]) == (1000, "intro", 3)
    assert _row(paths.state, MAPPED) is None, "a read must not write"


def test_first_edit_seeds_own_set_from_rekordbox_and_keeps_other_cues(
    client: TestClient, paths
) -> None:
    assert _save(client, MAPPED, "B", 20_000).status_code == 200
    slots = _slots(client, MAPPED)
    assert slots["A"]["cue"]["in_ms"] == 1000, "rekordbox hot cue A was dropped"
    assert slots["B"]["cue"]["in_ms"] == 20_000
    blob, source = _stored(paths.state, MAPPED)
    assert source == "webui"
    assert {(c["kind"], c["slot"], c["in_ms"]) for c in blob["cues"]} == {
        ("hot_cue", "A", 1000),
        ("hot_cue", "B", 20_000),
        ("memory", None, 64_000),
    }
    # And rekordbox's own table is untouched.
    assert [(c["slot"], c["in_ms"]) for c in rb_vendor.fetch_cues(VENDOR_ID)] == [
        ("A", 1000),
        (None, 64_000),
    ]


def test_clearing_a_rekordbox_cue_removes_it_from_the_own_set_only(
    client: TestClient,
) -> None:
    revision = _slots(client, MAPPED)["A"]["revision"]
    cleared = client.delete(
        f"/api/v1/tracks/{MAPPED}/hot-cues/A", headers={"If-Match": revision}
    )
    assert cleared.status_code == 200, cleared.text
    assert _slots(client, MAPPED)["A"]["cue"] is None
    assert any(c["slot"] == "A" for c in rb_vendor.fetch_cues(VENDOR_ID))


# ----- /anlz serves the own set ---------------------------------------------


def test_anlz_overlay_replaces_vendor_cues_only_when_an_own_row_exists(
    client: TestClient, paths
) -> None:
    vendor = {"cues": rb_vendor.fetch_cues(VENDOR_ID)}
    untouched = apply_own_cues(dict(vendor), MAPPED, paths.state)
    assert untouched["cues"] == vendor["cues"], "no own row: vendor cues must stand"

    _save(client, MAPPED, "B", 20_000)
    served = apply_own_cues(dict(vendor), MAPPED, paths.state)
    assert [(c["kind"], c["slot"]) for c in served["cues"]] == [
        ("hot_cue", "A"),
        ("hot_cue", "B"),
        ("memory", None),
    ]
    assert all("is_loop" in c for c in served["cues"])

    local = apply_own_cues({"cues": []}, LOCAL, paths.state)
    assert local["cues"] == []
    _save(client, LOCAL, "A", 5_000)
    assert apply_own_cues({"cues": []}, LOCAL, paths.state)["cues"][0]["in_ms"] == 5_000


# ----- ingest copies rekordbox cues in, and respects ownership ---------------


def _import(state: Path, sid: str, cues: list[dict[str, Any]]) -> bool:
    conn = state_db.open_rw(state)
    try:
        with StateWriter(conn, bus=FakeEventBus(), actor="test") as writer:
            changed = cue_store.import_vendor_cues(
                writer.set_field, conn, sid, cues, source="rekordbox", modified_at=MODIFIED_AT
            )
        conn.commit()
        return changed
    finally:
        conn.close()


HOT_A = {"kind": "hot_cue", "slot": "A", "in_ms": 1000}


def test_ingest_import_writes_refreshes_then_yields_to_user_edits(
    client: TestClient, paths
) -> None:
    assert _import(paths.state, MAPPED, [HOT_A]) is True
    assert _stored(paths.state, MAPPED)[1] == "rekordbox"

    # Overshoot control: a set nobody edited still follows rekordbox.
    assert _import(paths.state, MAPPED, [{**HOT_A, "in_ms": 2000}]) is True
    assert _stored(paths.state, MAPPED)[0]["cues"][0]["in_ms"] == 2000

    _save(client, MAPPED, "B", 20_000)
    assert _import(paths.state, MAPPED, [{**HOT_A, "in_ms": 9000}]) is False
    blob, source = _stored(paths.state, MAPPED)
    assert source == "webui"
    assert {c["slot"]: c["in_ms"] for c in blob["cues"]} == {"A": 2000, "B": 20_000}


def test_ingest_import_with_no_cues_and_no_row_writes_nothing(paths) -> None:
    assert _import(paths.state, LOCAL, []) is False
    assert _row(paths.state, LOCAL) is None


def test_rb_cue_rows_map_kinds_and_skip_unverified_or_deleted() -> None:
    def row(**kw: Any) -> SimpleNamespace:
        base = {"ContentID": "7", "InMsec": 0, "OutMsec": None, "Kind": 0,
                "ActiveLoop": 0, "BeatLoopSize": None, "ColorTableIndex": None,
                "Comment": None, "rb_local_deleted": 0}
        return SimpleNamespace(**{**base, **kw})

    db = SimpleNamespace(get_cue=lambda: [
        row(Kind=1, InMsec=500, Comment="a"),
        row(Kind=8, InMsec=600),
        row(Kind=0, InMsec=700),
        row(Kind=0, InMsec=800, OutMsec=1800, ActiveLoop=1),
        row(Kind=9, InMsec=900),
        row(Kind=2, InMsec=950, rb_local_deleted=1),
    ])
    cues = rb_ingest._rb_cues_by_content(db)["7"]
    assert [(c["kind"], c["slot"], c["in_ms"], c["out_ms"]) for c in cues] == [
        ("hot_cue", "A", 500, None),
        ("hot_cue", "H", 600, None),
        ("memory", None, 700, None),
        ("loop", None, 800, 1800),
    ]
