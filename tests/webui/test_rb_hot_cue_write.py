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
  - if a hot cue on a tagged MP3 is stored without its lead-in put back then broken (NAE-22)
  - if the slot API and fetch_cues disagree on a tagged MP3's cue then broken (NAE-22)
  - if a rekordbox cue inside the lead-in moves when re-saved where it reads then broken (NAE-22)
  - if a hot cue is saved on an MP3 whose lead-in cannot be read then broken (NAE-22)
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.webui.server import rb_vendor
from apps.webui.server.routes.rb_hot_cues import router
from apps.webui.server.sqlite_backend import make_backend

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]

VENDOR_ID = "126790091"
STABLE_ID = "a" * 40
VENDOR_ID_TWO = "126790092"
STABLE_ID_TWO = "b" * 40


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
        conn.execute(
            "INSERT INTO djmdContent (ID, Length) VALUES (?, ?)", (vendor_id, 300)
        )
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

def _slot_revision(slot: str) -> str:
    if len(slot) != 1 or slot not in rb_vendor.HOT_CUE_SLOTS:
        return "invalid-slot"
    return next(row["revision"] for row in rb_vendor.fetch_hot_cue_slots(VENDOR_ID) if row["slot"] == slot)


def _save(slot: str, in_ms: int, **kwargs: Any) -> dict[str, Any]:
    return rb_vendor.save_hot_cue(
        VENDOR_ID, slot, in_ms, expected_revision=_slot_revision(slot), **kwargs,
    )


def _clear(slot: str) -> dict[str, Any]:
    return rb_vendor.clear_hot_cue(
        VENDOR_ID, slot, expected_revision=_slot_revision(slot),
    )

@pytest.fixture
def master_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "master.plain.db"
    _make_master_plain_db(path, VENDOR_ID)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", path)
    return path


def test_save_hot_cue_inserts_new_row(master_db: Path) -> None:
    result = _save("C", 12_345, comment="drop")
    assert result["cue"]["slot"] == "C"
    assert result["cue"]["in_ms"] == 12_345
    assert result["cue"]["comment"] == "drop"
    assert isinstance(result["cue"]["revision"], str)
    cues = rb_vendor.fetch_cues(VENDOR_ID)
    assert len(cues) == 1
    assert cues[0]["slot"] == "C"
    assert cues[0]["in_ms"] == 12_345
    assert cues[0]["kind"] == "hot_cue"


def test_save_hot_cue_resave_overwrites_slot_not_duplicates(master_db: Path) -> None:
    _save("A", 1_000)
    _save("A", 5_000, comment="moved")
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
    _save("A", 1_000)
    assert "BEGIN IMMEDIATE" in statements


def test_save_hot_cue_distinct_slots_coexist(master_db: Path) -> None:
    _save("A", 1_000)
    _save("H", 2_000)
    cues = {c["slot"]: c for c in rb_vendor.fetch_cues(VENDOR_ID)}
    assert set(cues) == {"A", "H"}


def test_save_hot_cue_rejects_negative_position(master_db: Path) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _save("A", -1)
    assert exc.value.detail["code"] == "INVALID_CUE_POSITION"


@pytest.mark.parametrize("slot", ["I", "Z", "9", ""])
def test_save_hot_cue_rejects_unknown_slot(master_db: Path, slot: str) -> None:
    with pytest.raises(rb_vendor.HotCueSlotError):
        _save(slot, 1_000)


def test_clear_hot_cue_soft_deletes(master_db: Path) -> None:
    _save("B", 1_000)
    assert len(rb_vendor.fetch_cues(VENDOR_ID)) == 1
    _clear("B")
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
    _clear("D")  # must not raise
    assert rb_vendor.fetch_cues(VENDOR_ID) == []


def test_clear_hot_cue_acquires_immediate_write_transaction(
    master_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save("A", 1_000)
    statements: list[str] = []
    open_rw = rb_vendor._open_rw

    def open_traced(path: Path, label: str) -> sqlite3.Connection:
        conn = open_rw(path, label)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(rb_vendor, "_open_rw", open_traced)
    _clear("A")
    assert "BEGIN IMMEDIATE" in statements


def test_clear_hot_cue_rejects_unknown_slot(master_db: Path) -> None:
    with pytest.raises(rb_vendor.HotCueSlotError):
        _clear("Z")


def test_save_hot_cue_after_clear_reinserts(master_db: Path) -> None:
    _save("E", 1_000)
    _clear("E")
    _save("E", 9_000)
    cues = rb_vendor.fetch_cues(VENDOR_ID)
    assert len(cues) == 1
    assert cues[0]["in_ms"] == 9_000


def test_stale_save_rejects_without_mutating(master_db: Path) -> None:
    stale_revision = _slot_revision("A")
    _save("A", 1_000, comment="new")
    with pytest.raises(Exception) as exc:
        rb_vendor.save_hot_cue(
            VENDOR_ID, "A", 2_000, expected_revision=stale_revision,
        )
    assert getattr(exc.value, "status_code", None) == 409
    assert rb_vendor.fetch_cues(VENDOR_ID)[0]["in_ms"] == 1_000


def test_save_rejects_position_beyond_server_track_duration(master_db: Path) -> None:
    with pytest.raises(Exception) as exc:
        _save("A", 300_001)
    assert getattr(exc.value, "detail", {})["code"] == "INVALID_CUE_POSITION"


def test_restore_reinstates_overwritten_loop_preimage(master_db: Path) -> None:
    _save("A", 1_000, comment="drop", color_table_index=4)
    conn = sqlite3.connect(str(master_db))
    try:
        conn.execute(
            "UPDATE djmdCue SET OutMsec = ?, OutFrame = ?, ActiveLoop = ?, "
            "BeatLoopSize = ? WHERE ContentID = ?",
            (2_000, 882, 1, 4, VENDOR_ID),
        )
        conn.commit()
    finally:
        conn.close()
    result = _save("A", 3_000, comment="move")
    rb_vendor.restore_hot_cue(
        VENDOR_ID,
        "A",
        expected_revision=result["cue"]["revision"],
        reversal_id=result["reversal"]["reversal_id"],
    )
    restored = rb_vendor.fetch_cues(VENDOR_ID)[0]
    assert restored["in_ms"] == 1_000
    assert restored["out_ms"] == 2_000
    assert restored["active_loop"] is True
    assert restored["beat_loop_size"] == 4
    assert restored["color_table_index"] == 4
    assert restored["comment"] == "drop"


# ----- route-level tests (TestClient over the real HTTP contract) -----------

@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    master_path = tmp_path / "master.plain.db"
    state_path = tmp_path / "state.db"
    _make_master_plain_db(master_path, VENDOR_ID)
    _make_state_db(state_path, STABLE_ID, VENDOR_ID)
    master = sqlite3.connect(str(master_path))
    try:
        master.execute(
            "INSERT INTO djmdContent (ID, Length) VALUES (?, ?)", (VENDOR_ID_TWO, 300),
        )
        master.commit()
    finally:
        master.close()
    state = state_db.open_rw(state_path)
    try:
        state.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, created_at, updated_at) "
            "VALUES (?, 'inferred', '2026-01-01', '2026-01-01')", (STABLE_ID_TWO,),
        )
        state.execute(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
            "VALUES (?, 'rekordbox', ?)", (STABLE_ID_TWO, VENDOR_ID_TWO),
        )
        state.commit()
    finally:
        state.close()
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_path)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)

    app = FastAPI()
    app.state.backend = make_backend()
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


def _client_revision(client: TestClient, slot: str) -> str:
    response = client.get(f"/api/v1/tracks/{STABLE_ID}/hot-cues")
    assert response.status_code == 200, response.text
    return next(row["revision"] for row in response.json() if row["slot"] == slot)


def _put(client: TestClient, slot: str, body: dict[str, Any]):
    return client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/{slot}",
        json=body,
        headers={"If-Match": _client_revision(client, slot)},
    )


def _delete(client: TestClient, slot: str):
    return client.delete(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/{slot}",
        headers={"If-Match": _client_revision(client, slot)},
    )


def test_put_hot_cue_saves_and_reads_back(client: TestClient) -> None:
    resp = _put(client, "A", {"in_ms": 4_200, "comment": "intro"})
    assert resp.status_code == 200, resp.text
    body = resp.json()["cue"]
    assert body["slot"] == "A"
    assert body["in_ms"] == 4_200
    assert body["comment"] == "intro"
    assert rb_vendor.fetch_cues(VENDOR_ID)[0]["in_ms"] == 4_200


def test_put_hot_cue_resave_overwrites(client: TestClient) -> None:
    _put(client, "B", {"in_ms": 1_000})
    resp = _put(client, "B", {"in_ms": 2_000})
    assert resp.status_code == 200
    cues = [c for c in rb_vendor.fetch_cues(VENDOR_ID) if c["slot"] == "B"]
    assert len(cues) == 1
    assert cues[0]["in_ms"] == 2_000


def test_delete_hot_cue_clears_slot(client: TestClient) -> None:
    _put(client, "C", {"in_ms": 1_000})
    resp = _delete(client, "C")
    assert resp.status_code == 200
    assert rb_vendor.fetch_cues(VENDOR_ID) == []


def test_put_hot_cue_rejects_negative_ms_422(client: TestClient) -> None:
    resp = _put(client, "A", {"in_ms": -5})
    assert resp.status_code == 422


def test_put_hot_cue_unknown_stable_id_404(client: TestClient) -> None:
    resp = client.put(
        f"/api/v1/tracks/{'0' * 40}/hot-cues/A", json={"in_ms": 1_000},
        headers={"If-Match": "irrelevant"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "TRACK_NOT_FOUND"


@pytest.mark.parametrize("slot", ["I", "9", "AB", "0"])
def test_put_hot_cue_slot_beyond_h_is_unreachable(
    client: TestClient, slot: str
) -> None:
    """Kind 9-11 must never be reachable through this API (unverified)."""
    resp = client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/{slot}", json={"in_ms": 1_000},
        headers={"If-Match": "irrelevant"},
    )
    assert resp.status_code == 422


def test_http_requires_current_revision_and_returns_conflict_revision(client: TestClient) -> None:
    missing = client.put(f"/api/v1/tracks/{STABLE_ID}/hot-cues/A", json={"in_ms": 1_000})
    assert missing.status_code == 428
    stale = _client_revision(client, "A")
    _put(client, "A", {"in_ms": 1_000})
    conflict = client.delete(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/A", headers={"If-Match": stale},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current_revision"] == _client_revision(client, "A")
    assert rb_vendor.fetch_cues(VENDOR_ID)[0]["in_ms"] == 1_000


def test_http_restore_uses_reversal_preimage(client: TestClient) -> None:
    saved = _put(client, "D", {"in_ms": 1_000, "comment": "intro"})
    overwritten = _put(client, "D", {"in_ms": 2_000, "comment": "drop"})
    restore = client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/D/restore",
        json={"reversal_id": overwritten.json()["reversal"]["reversal_id"]},
        headers={"If-Match": overwritten.headers["ETag"]},
    )
    assert restore.status_code == 200, restore.text
    assert restore.json()["cue"]["in_ms"] == saved.json()["cue"]["in_ms"]
    assert restore.json()["cue"]["comment"] == "intro"


def test_restore_rejects_cross_slot_and_cross_track_tokens(client: TestClient) -> None:
    saved = _put(client, "A", {"in_ms": 1_000})
    token = saved.json()["reversal"]["reversal_id"]
    cross_slot = client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/B/restore",
        json={"reversal_id": token},
        headers={"If-Match": _client_revision(client, "B")},
    )
    assert cross_slot.status_code == 409
    cross_track = client.put(
        f"/api/v1/tracks/{STABLE_ID_TWO}/hot-cues/A/restore",
        json={"reversal_id": token},
        headers={"If-Match": _client_revision(client, "A")},
    )
    assert cross_track.status_code == 409
    assert rb_vendor.fetch_cues(VENDOR_ID)[0]["in_ms"] == 1_000


def test_restore_rejects_forged_preimage_fields_and_consumes_token(client: TestClient) -> None:
    first = _put(client, "E", {"in_ms": 1_000, "comment": "intro"})
    overwrite = _put(client, "E", {"in_ms": 2_000, "comment": "drop"})
    token = overwrite.json()["reversal"]["reversal_id"]
    forged = client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/E/restore",
        json={
            "reversal_id": token,
            "preimage": {"in_ms": 299_999, "comment": "forged", "active_loop": True},
        },
        headers={"If-Match": overwrite.headers["ETag"]},
    )
    assert forged.status_code == 422
    restore = client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/E/restore",
        json={"reversal_id": token},
        headers={"If-Match": overwrite.headers["ETag"]},
    )
    assert restore.status_code == 200, restore.text
    assert restore.json()["cue"]["in_ms"] == first.json()["cue"]["in_ms"]
    assert restore.json()["cue"]["comment"] == "intro"
    replay = client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/E/restore",
        json={"reversal_id": token},
        headers={"If-Match": restore.headers["ETag"]},
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "HOT_CUE_REVERSAL_CONSUMED"


def test_restore_rejects_token_after_later_slot_mutation(client: TestClient) -> None:
    first = _put(client, "F", {"in_ms": 1_000})
    later = _put(client, "F", {"in_ms": 2_000})
    stale = client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/F/restore",
        json={"reversal_id": first.json()["reversal"]["reversal_id"]},
        headers={"If-Match": later.headers["ETag"]},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "HOT_CUE_REVERSAL_STALE"


def test_slot_generation_rejects_aba_restore_after_same_empty_snapshot(client: TestClient) -> None:
    _put(client, "G", {"in_ms": 1_000, "comment": "X"})
    clear_x = _delete(client, "G")
    token = clear_x.json()["reversal"]["reversal_id"]
    _put(client, "G", {"in_ms": 2_000, "comment": "Y"})
    clear_y = _delete(client, "G")
    stale_restore = client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/G/restore",
        json={"reversal_id": token},
        headers={"If-Match": clear_y.headers["ETag"]},
    )
    assert stale_restore.status_code == 409
    assert stale_restore.json()["detail"]["code"] == "HOT_CUE_REVERSAL_STALE"
    assert next(row for row in client.get(f"/api/v1/tracks/{STABLE_ID}/hot-cues").json() if row["slot"] == "G")["cue"] is None


def test_initial_empty_revision_cannot_write_after_save_clear_cycle(client: TestClient) -> None:
    initial_revision = _client_revision(client, "H")
    first = client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/H",
        json={"in_ms": 1_000},
        headers={"If-Match": initial_revision},
    )
    assert first.status_code == 200
    cleared = client.delete(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/H",
        headers={"If-Match": first.headers["ETag"]},
    )
    assert cleared.status_code == 200
    assert _client_revision(client, "H") != initial_revision
    stale = client.put(
        f"/api/v1/tracks/{STABLE_ID}/hot-cues/H",
        json={"in_ms": 2_000},
        headers={"If-Match": initial_revision},
    )
    assert stale.status_code == 409
    assert next(row for row in client.get(f"/api/v1/tracks/{STABLE_ID}/hot-cues").json() if row["slot"] == "H")["cue"] is None


# ----- MP3 lead-in: the slot API is on our timeline too (NAE-22) -------------

TAGGED_MP3 = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup" / "src-128.mp3"


def _set_folder(master_db: Path, folder: Path = TAGGED_MP3) -> None:
    conn = sqlite3.connect(str(master_db))
    try:
        conn.execute("UPDATE djmdContent SET FolderPath = ? WHERE ID = ?", (str(folder), VENDOR_ID))
        conn.commit()
    finally:
        conn.close()


def _stored_slot_a(master_db: Path) -> int:
    conn = sqlite3.connect(str(master_db))
    try:
        sql = "SELECT InMsec FROM djmdCue WHERE ContentID = ? AND Kind = 1 AND rb_local_deleted = 0"
        return int(conn.execute(sql, (VENDOR_ID,)).fetchone()[0])
    finally:
        conn.close()


@pytest.mark.requirement("NAE-22")
def test_hot_cue_slots_round_trip_on_our_timeline_for_a_tagged_mp3(
    master_db: Path,
) -> None:
    """Saved where the deck plays it, stored where rekordbox plays it, read back unmoved."""
    _set_folder(master_db)
    # 1105 samples at 22.05 kHz, put back on the way into rekordbox.
    assert _save("A", 1_000)["cue"]["in_ms"] == 1_000
    assert _stored_slot_a(master_db) == 1_050
    slot_a = next(r for r in rb_vendor.fetch_hot_cue_slots(VENDOR_ID) if r["slot"] == "A")
    assert slot_a["cue"]["in_ms"] == 1_000
    assert [c["in_ms"] for c in rb_vendor.fetch_cues(VENDOR_ID)] == [1_000]
    _save("A", slot_a["cue"]["in_ms"])
    again = next(r for r in rb_vendor.fetch_hot_cue_slots(VENDOR_ID) if r["slot"] == "A")
    assert again["cue"]["in_ms"] == 1_000, "re-saving a cue where it reads must not move it"


@pytest.mark.requirement("NAE-22")
def test_a_rekordbox_cue_inside_the_lead_in_survives_a_re_save(master_db: Path) -> None:
    """A cue rekordbox put at 5 ms reads as 0 here; re-saving it at 0 keeps 5 ms."""
    _set_folder(master_db)
    conn = sqlite3.connect(str(master_db))
    try:
        conn.execute(
            "INSERT INTO djmdCue (ID, ContentID, InMsec, OutMsec, Kind, ActiveLoop) "
            "VALUES ('early', ?, 5, -1, 1, 0)",
            (VENDOR_ID,),
        )
        conn.commit()
    finally:
        conn.close()
    slot_a = next(r for r in rb_vendor.fetch_hot_cue_slots(VENDOR_ID) if r["slot"] == "A")
    assert slot_a["cue"]["in_ms"] == 0
    _save("A", 0, comment="renamed")
    assert _stored_slot_a(master_db) == 5, "an unmoved early cue must keep rekordbox's time"
    # Control: a cue actually moved to 0 on our timeline lands at the lead-in.
    _save("A", 10)
    _save("A", 0)
    assert _stored_slot_a(master_db) == 50


@pytest.mark.requirement("NAE-22")
def test_a_hot_cue_save_on_an_unreadable_mp3_is_refused(master_db: Path, tmp_path: Path) -> None:
    """Without the file, the lead-in is unknown; a guessed 0 would land the cue early later."""
    _set_folder(master_db, tmp_path / "not-here.mp3")
    with pytest.raises(HTTPException) as exc:
        _save("A", 1_000)
    assert exc.value.status_code == 409
    assert [c["in_ms"] for c in rb_vendor.fetch_cues(VENDOR_ID)] == []
    # Control: a missing WAV has no lead-in to read, so its save goes through as is.
    _set_folder(master_db, tmp_path / "not-here.wav")
    assert _save("A", 1_000)["cue"]["in_ms"] == 1_000
    assert _stored_slot_a(master_db) == 1_000
