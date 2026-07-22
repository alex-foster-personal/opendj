"""Hot-cue SAVE/CLEAR write-surface tests (edit-write-path lane).

Cloud-buildable: builds a synthetic state.db (apps.shared.state.db) +
minimal master.plain.db djmdContent/djmdCue schema in tmp_path -- no real
data/master.plain.db or data/state/state.db needed, so these run in CI and
in cloud sandboxes with no local rekordbox library (hybrid tier: Kind 9-11
mapping + the audible cue jump still need a local-verify pass separately,
see PARITY-TODO.md "Hot-cue SAVE").

Regression one-liners:
  - if save_hot_cue can't insert a fresh Kind 1-8 row then broken
  - if re-saving the same slot inserts a second row instead of overwriting then broken
  - if clear_hot_cue doesn't soft-delete (rb_local_deleted) then broken
  - if a cleared slot still shows up in fetch_cues then broken
  - if a slot outside A-H is ever accepted then broken
  - if a negative in_ms is ever accepted then broken
  - if a hot-cue write uses a deferred SQLite transaction then concurrent saves can duplicate a slot
  - if the PUT/DELETE routes don't round-trip through fetch_cues then broken
  - if the route ever accepts a slot letter beyond H (Kind 9-11) then broken
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server import rb_vendor
from apps.webui.server.routes.rb_hot_cues import router
from apps.webui.server.sqlite_backend import make_backend

pytestmark = pytest.mark.requirement("CAT-05")

VENDOR_ID = "126790091"
STABLE_ID = "a" * 40


def _make_master_plain_db(path: Path, vendor_id: str) -> None:
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
        conn.execute("INSERT INTO djmdContent (ID) VALUES (?)", (vendor_id,))
        conn.commit()
    finally:
        conn.close()


def _make_state_db(path: Path, stable_id: str, vendor_id: str) -> None:
    conn = state_db.open_rw(path)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, created_at, "
            "updated_at) VALUES (?, 'inferred', '2026-01-01', '2026-01-01')",
            (stable_id,),
        )
        conn.execute(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
            "VALUES (?, 'rekordbox', ?)",
            (stable_id, vendor_id),
        )
        conn.commit()
    finally:
        conn.close()


# ----- pure rb_vendor unit tests (no HTTP layer) -----------------------------

@pytest.fixture
def master_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "master.plain.db"
    _make_master_plain_db(path, VENDOR_ID)
    monkeypatch.setattr(rb_vendor, "MASTER_PLAIN_DB", path)
    return path


def test_save_hot_cue_inserts_new_row(master_db: Path) -> None:
    row = rb_vendor.save_hot_cue(VENDOR_ID, "C", 12_345, comment="drop")
    assert row == {
        "kind": "hot_cue", "slot": "C", "in_ms": 12_345, "out_ms": None,
        "is_loop": False, "active_loop": False, "beat_loop_size": None,
        "color_table_index": None, "comment": "drop",
    }
    cues = rb_vendor.fetch_cues(VENDOR_ID)
    assert len(cues) == 1
    assert cues[0]["slot"] == "C"
    assert cues[0]["in_ms"] == 12_345
    assert cues[0]["kind"] == "hot_cue"


def test_save_hot_cue_resave_overwrites_slot_not_duplicates(master_db: Path) -> None:
    rb_vendor.save_hot_cue(VENDOR_ID, "A", 1_000)
    rb_vendor.save_hot_cue(VENDOR_ID, "A", 5_000, comment="moved")
    cues = rb_vendor.fetch_cues(VENDOR_ID)
    assert len(cues) == 1, "slot conflict must overwrite, never duplicate"
    assert cues[0]["in_ms"] == 5_000
    assert cues[0]["comment"] == "moved"


def test_save_hot_cue_acquires_immediate_write_transaction(
    master_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statements: list[str] = []
    open_rw = rb_vendor._open_rw

    def open_traced(path: Path, label: str) -> sqlite3.Connection:
        conn = open_rw(path, label)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(rb_vendor, "_open_rw", open_traced)
    rb_vendor.save_hot_cue(VENDOR_ID, "A", 1_000)
    assert "BEGIN IMMEDIATE" in statements


def test_save_hot_cue_distinct_slots_coexist(master_db: Path) -> None:
    rb_vendor.save_hot_cue(VENDOR_ID, "A", 1_000)
    rb_vendor.save_hot_cue(VENDOR_ID, "H", 2_000)
    cues = {c["slot"]: c for c in rb_vendor.fetch_cues(VENDOR_ID)}
    assert set(cues) == {"A", "H"}


def test_save_hot_cue_rejects_negative_position(master_db: Path) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        rb_vendor.save_hot_cue(VENDOR_ID, "A", -1)
    assert exc.value.detail["code"] == "INVALID_CUE_POSITION"


@pytest.mark.parametrize("slot", ["I", "Z", "9", ""])
def test_save_hot_cue_rejects_unknown_slot(master_db: Path, slot: str) -> None:
    with pytest.raises(rb_vendor.HotCueSlotError):
        rb_vendor.save_hot_cue(VENDOR_ID, slot, 1_000)


def test_clear_hot_cue_soft_deletes(master_db: Path) -> None:
    rb_vendor.save_hot_cue(VENDOR_ID, "B", 1_000)
    assert len(rb_vendor.fetch_cues(VENDOR_ID)) == 1
    rb_vendor.clear_hot_cue(VENDOR_ID, "B")
    assert rb_vendor.fetch_cues(VENDOR_ID) == []
    conn = sqlite3.connect(str(master_db))
    try:
        row = conn.execute(
            "SELECT rb_local_deleted FROM djmdCue WHERE ContentID = ?",
            (VENDOR_ID,),
        ).fetchone()
    finally:
        conn.close()
    assert row == (1,), "clear must soft-delete, not hard-delete"


def test_clear_hot_cue_on_empty_slot_is_a_noop(master_db: Path) -> None:
    rb_vendor.clear_hot_cue(VENDOR_ID, "D")  # must not raise
    assert rb_vendor.fetch_cues(VENDOR_ID) == []


def test_clear_hot_cue_acquires_immediate_write_transaction(
    master_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rb_vendor.save_hot_cue(VENDOR_ID, "A", 1_000)
    statements: list[str] = []
    open_rw = rb_vendor._open_rw

    def open_traced(path: Path, label: str) -> sqlite3.Connection:
        conn = open_rw(path, label)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(rb_vendor, "_open_rw", open_traced)
    rb_vendor.clear_hot_cue(VENDOR_ID, "A")
    assert "BEGIN IMMEDIATE" in statements


def test_clear_hot_cue_rejects_unknown_slot(master_db: Path) -> None:
    with pytest.raises(rb_vendor.HotCueSlotError):
        rb_vendor.clear_hot_cue(VENDOR_ID, "Z")


def test_save_hot_cue_after_clear_reinserts(master_db: Path) -> None:
    rb_vendor.save_hot_cue(VENDOR_ID, "E", 1_000)
    rb_vendor.clear_hot_cue(VENDOR_ID, "E")
    rb_vendor.save_hot_cue(VENDOR_ID, "E", 9_000)
    cues = rb_vendor.fetch_cues(VENDOR_ID)
    assert len(cues) == 1
    assert cues[0]["in_ms"] == 9_000


# ----- route-level tests (TestClient over the real HTTP contract) -----------

@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    master_path = tmp_path / "master.plain.db"
    state_path = tmp_path / "state.db"
    _make_master_plain_db(master_path, VENDOR_ID)
    _make_state_db(state_path, STABLE_ID, VENDOR_ID)
    monkeypatch.setattr(rb_vendor, "MASTER_PLAIN_DB", master_path)
    monkeypatch.setattr(rb_vendor, "STATE_DB", state_path)

    app = FastAPI()
    app.state.backend = make_backend()
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


def test_put_hot_cue_saves_and_reads_back(client: TestClient) -> None:
    resp = client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/A",
        json={"in_ms": 4_200, "comment": "intro"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slot"] == "A"
    assert body["in_ms"] == 4_200
    assert body["comment"] == "intro"
    assert rb_vendor.fetch_cues(VENDOR_ID)[0]["in_ms"] == 4_200


def test_put_hot_cue_resave_overwrites(client: TestClient) -> None:
    client.put(f"/api/v1/tracks/{STABLE_ID}/hot-cues/B", json={"in_ms": 1_000})
    resp = client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/B", json={"in_ms": 2_000}
    )
    assert resp.status_code == 200
    cues = [c for c in rb_vendor.fetch_cues(VENDOR_ID) if c["slot"] == "B"]
    assert len(cues) == 1
    assert cues[0]["in_ms"] == 2_000


def test_delete_hot_cue_clears_slot(client: TestClient) -> None:
    client.put(f"/api/v1/tracks/{STABLE_ID}/hot-cues/C", json={"in_ms": 1_000})
    resp = client.delete(f"/api/v1/tracks/{STABLE_ID}/hot-cues/C")
    assert resp.status_code == 204
    assert rb_vendor.fetch_cues(VENDOR_ID) == []


def test_put_hot_cue_rejects_negative_ms_422(client: TestClient) -> None:
    resp = client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/A", json={"in_ms": -5}
    )
    assert resp.status_code == 422


def test_put_hot_cue_unknown_stable_id_404(client: TestClient) -> None:
    resp = client.put(
        f"/api/v1/tracks/{'0' * 40}/hot-cues/A", json={"in_ms": 1_000}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "TRACK_NOT_FOUND"


@pytest.mark.parametrize("slot", ["I", "9", "AB", "0"])
def test_put_hot_cue_slot_beyond_h_is_unreachable(
    client: TestClient, slot: str
) -> None:
    """Kind 9-11 must never be reachable through this API (unverified)."""
    resp = client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/{slot}", json={"in_ms": 1_000}
    )
    assert resp.status_code == 422
