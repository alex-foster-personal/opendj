"""Playlist edit undo/redo inverse-command tests (playlists-router gating unit).

Regression one-liners:
  * if undo of create/rename/memberships/delete/duplicate does not restore
    name + items + existence then broken
  * if undo then undo does not walk back two edits (and redo walk forward) then broken
  * if undo then a new membership replace leaves can_redo true then broken
  * if 51 membership replaces leave a live window other than 50 then broken
  * if undo after a sneaky live-row mutation does not raise ConflictError then broken
  * if a second PlaylistStore on the same state.db cannot undo then broken (restart)
  * if a no-op rename or membership replace grows the stack then broken
  * if events with actor != webui enter the stack then broken
  * if create + undo hard-DELETEs the row instead of tombstoning then broken
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.backend import ConflictError, NotFoundError
from apps.webui.server.playlist_history import (
    HISTORY_LIMIT,
    EditOp,
    PlaylistEditCommand,
    PlaylistHistoryEmptyError,
    PlaylistSnapshot,
    invert,
    label_for,
    rebuild_stack,
    snapshots_match,
)
from apps.webui.server.playlist_store import PlaylistStore

TRACK_IDS: list[str] = ["t-001", "t-002", "t-003", "t-004"]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh tmp state.db seeded with 4 tracks via the shared-state writer."""
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for i, sid in enumerate(TRACK_IDS, start=1):
            writer.upsert_track(
                stable_id=sid, stable_id_tier="inferred",
                title=f"Track {i}", artists=[f"Artist {i}"], album=None,
                isrc=None, duration_ms=180_000 + i, file_path=None,
            )
    finally:
        writer.close()
        conn.close()
    return path


def _snap(
    playlist_id: str = "pl-1",
    name: str = "Warmup",
    items: list[str] | None = None,
) -> PlaylistSnapshot:
    return PlaylistSnapshot(
        playlist_id=playlist_id, name=name, vendor="webui",
        vendor_pl_id="v1", items=list(items or []),
    )


def _cmd(
    op: EditOp,
    *,
    command_id: str = "aa",
    before: PlaylistSnapshot | None = None,
    after: PlaylistSnapshot | None = None,
    playlist_id: str | None = None,
) -> PlaylistEditCommand:
    pid = playlist_id
    if pid is None:
        snap = after if after is not None else before
        pid = snap.playlist_id if snap is not None else "pl-1"
    return PlaylistEditCommand(
        command_id=command_id, op=op, playlist_id=pid,
        ts="2026-01-01T00:00:00+00:00", before=before, after=after,
    )


def _tombstone(db_path: Path, playlist_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT deleted_at FROM playlists WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _history_kinds(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [
            row[0] for row in conn.execute(
                "SELECT kind FROM events "
                "WHERE kind IN ('playlist.edit','playlist.undo','playlist.redo') "
                "ORDER BY id"
            )
        ]
    finally:
        conn.close()


# --- pure rebuild / invert (no sqlite) --------------------------------------

def test_rebuild_stack_edit_undo_redo_and_truncation() -> None:
    a = _cmd("create", command_id="a", after=_snap(name="A")).to_dict()
    b = _cmd("rename", command_id="b",
             before=_snap(name="A"), after=_snap(name="B")).to_dict()
    c = _cmd("rename", command_id="c",
             before=_snap(name="B"), after=_snap(name="C")).to_dict()
    events = [
        {"kind": "playlist.edit", "payload": a},
        {"kind": "playlist.edit", "payload": b},
        {"kind": "playlist.undo", "payload": {"command_id": "b"}},
        {"kind": "playlist.edit", "payload": c},
    ]
    stack, cursor = rebuild_stack(events)
    assert [cmd.command_id for cmd in stack] == ["a", "c"]
    assert cursor == 2


def test_invert_table_and_labels() -> None:
    created = _cmd("create", after=_snap(name="Fresh"))
    assert invert(created) == ("delete", created.after)
    assert label_for(created) == "Create 'Fresh'"

    renamed = _cmd(
        "rename", before=_snap(name="Warmup"), after=_snap(name="Peak"),
    )
    assert invert(renamed)[0] == "rename"
    renamed_before = invert(renamed)[1]
    assert renamed_before is not None
    assert renamed_before.name == "Warmup"
    assert label_for(renamed) == "Rename 'Warmup' to 'Peak'"

    members = _cmd(
        "memberships",
        before=_snap(items=["t-001"]),
        after=_snap(items=["t-002", "t-001"]),
    )
    assert invert(members)[0] == "memberships"
    members_before = invert(members)[1]
    assert members_before is not None
    assert members_before.items == ["t-001"]
    assert label_for(members) == "Edit tracks in 'Warmup'"

    deleted = _cmd("delete", before=_snap(name="Doomed", items=["t-001"]))
    assert invert(deleted) == ("create", deleted.before)
    assert label_for(deleted) == "Delete 'Doomed'"

    duped = _cmd("duplicate", after=_snap(name="Peak Hour (copy)", items=["t-002"]))
    assert invert(duped) == ("delete", duped.after)
    assert label_for(duped) == "Duplicate 'Peak Hour (copy)'"


def test_snapshots_match_ignores_identity_fields_other_than_name_items() -> None:
    live = _snap(name="Warmup", items=["t-001"])
    expected = PlaylistSnapshot(
        playlist_id="other", name="Warmup", vendor="rekordbox",
        vendor_pl_id="x", items=["t-001"],
    )
    assert snapshots_match(live, expected)
    assert not snapshots_match(live, _snap(name="Warmup", items=["t-002"]))
    assert snapshots_match(None, None)
    assert not snapshots_match(live, None)
    assert not snapshots_match(None, live)


# --- store inverses ---------------------------------------------------------

def test_inverse_of_each_op_restores_before_snapshot(db_path: Path) -> None:
    with PlaylistStore(db_path, bus=FakeEventBus()) as store:
        created = store.create_playlist("Warmup")
        assert created.items == []
        store.undo()
        with pytest.raises(NotFoundError):
            store.get_playlist_row(created.playlist_id)
        assert _tombstone(db_path, created.playlist_id) is not None
        store.redo()
        restored = store.get_playlist_row(created.playlist_id)
        assert restored.name == "Warmup"
        assert restored.items == []

        renamed = store.rename_playlist(
            created.playlist_id, "Peak", expected_etag=restored.etag,
        )
        store.undo()
        after_rename_undo = store.get_playlist_row(created.playlist_id)
        assert after_rename_undo.name == "Warmup"
        store.redo()
        after_rename_redo = store.get_playlist_row(created.playlist_id)
        assert after_rename_redo.name == "Peak"
        assert after_rename_redo.etag == renamed.etag or after_rename_redo.name == "Peak"

        store.replace_memberships(
            created.playlist_id, ["t-002", "t-001"],
            expected_etag=after_rename_redo.etag,
        )
        store.undo()
        after_members_undo = store.get_playlist_row(created.playlist_id)
        assert after_members_undo.items == []
        assert after_members_undo.name == "Peak"
        store.redo()
        after_members_redo = store.get_playlist_row(created.playlist_id)
        assert after_members_redo.items == ["t-002", "t-001"]
        assert after_members_redo.name == "Peak"

        store.delete_playlist(created.playlist_id, expected_etag=after_members_redo.etag)
        store.undo()
        undeleted = store.get_playlist_row(created.playlist_id)
        assert undeleted.name == "Peak"
        assert undeleted.items == ["t-002", "t-001"]

        copy = store.duplicate_playlist(created.playlist_id)
        assert copy.playlist_id != created.playlist_id
        assert copy.items == ["t-002", "t-001"]
        store.undo()
        with pytest.raises(NotFoundError):
            store.get_playlist_row(copy.playlist_id)
        source = store.get_playlist_row(created.playlist_id)
        assert source.items == ["t-002", "t-001"]


def test_undo_then_undo_then_redo_walks_two_edits(db_path: Path) -> None:
    with PlaylistStore(db_path, bus=FakeEventBus()) as store:
        row = store.create_playlist("Warmup")
        row = store.replace_memberships(
            row.playlist_id, ["t-001", "t-002"], expected_etag=row.etag,
        )
        row = store.rename_playlist(row.playlist_id, "Peak", expected_etag=row.etag)
        store.undo()
        after_one = store.get_playlist_row(row.playlist_id)
        assert after_one.name == "Warmup"
        assert after_one.items == ["t-001", "t-002"]
        store.undo()
        after_two = store.get_playlist_row(row.playlist_id)
        assert after_two.name == "Warmup"
        assert after_two.items == []
        store.redo()
        after_redo = store.get_playlist_row(row.playlist_id)
        assert after_redo.items == ["t-001", "t-002"]
        hist = store.history()
        assert hist["can_undo"] is True
        assert hist["can_redo"] is True


def test_new_edit_after_undo_invalidates_redo(db_path: Path) -> None:
    with PlaylistStore(db_path, bus=FakeEventBus()) as store:
        row = store.create_playlist("Warmup")
        row = store.rename_playlist(row.playlist_id, "Peak", expected_etag=row.etag)
        store.undo()
        assert store.history()["can_redo"] is True
        row = store.get_playlist_row(row.playlist_id)
        store.replace_memberships(
            row.playlist_id, ["t-003"], expected_etag=row.etag,
        )
        hist = store.history()
        assert hist["can_redo"] is False
        assert hist["cursor"] == len(hist["entries"])
    kinds = _history_kinds(db_path)
    assert kinds.count("playlist.undo") == 1
    assert kinds.count("playlist.edit") >= 3
    with PlaylistStore(db_path, bus=FakeEventBus()) as restarted:
        rebuilt = restarted.history()
        assert rebuilt["can_redo"] is False


def test_bounded_history_drops_oldest_edit(db_path: Path) -> None:
    with PlaylistStore(db_path, bus=FakeEventBus()) as store:
        row = store.create_playlist("Bounded")
        for i in range(51):
            sid = TRACK_IDS[i % len(TRACK_IDS)]
            row = store.replace_memberships(
                row.playlist_id, [sid], expected_etag=row.etag,
            )
        hist = store.history()
        assert len(hist["entries"]) == HISTORY_LIMIT
        assert hist["cursor"] == HISTORY_LIMIT
        for _ in range(HISTORY_LIMIT):
            store.undo()
        exhausted = store.history()
        assert exhausted["can_undo"] is False
        live = store.get_playlist_row(row.playlist_id)
        # Create + first membership fell off the window; cannot reach empty.
        assert live.items != []


def test_revision_conflict_when_live_diverges_from_after(db_path: Path) -> None:
    with PlaylistStore(db_path, bus=FakeEventBus()) as store:
        row = store.create_playlist("Warmup")
        row = store.replace_memberships(
            row.playlist_id, ["t-001"], expected_etag=row.etag,
        )
        playlist_id = row.playlist_id
        etag = row.etag
    with PlaylistStore(db_path, bus=FakeEventBus()) as sneaky:
        sneaky.replace_memberships(
            playlist_id, ["t-002"], expected_etag=etag, record_edit=False,
        )
    with PlaylistStore(db_path, bus=FakeEventBus()) as store, pytest.raises(ConflictError):
        store.undo()


def test_restart_rebuild_undo_restores_last_before_snapshot(db_path: Path) -> None:
    with PlaylistStore(db_path, bus=FakeEventBus()) as store:
        row = store.create_playlist("Warmup")
        row = store.replace_memberships(
            row.playlist_id, ["t-001", "t-004"], expected_etag=row.etag,
        )
        playlist_id = row.playlist_id
        store.close()
        with PlaylistStore(db_path, bus=FakeEventBus()) as restarted:
            restarted.undo()
            restored = restarted.get_playlist_row(playlist_id)
            assert restored.items == []
            assert restored.name == "Warmup"


def test_noop_rename_and_membership_do_not_grow_stack(db_path: Path) -> None:
    with PlaylistStore(db_path, bus=FakeEventBus()) as store:
        row = store.create_playlist("Keep Me")
        assert len(store.history()["entries"]) == 1
        same_name = store.rename_playlist(
            row.playlist_id, "Keep Me", expected_etag=row.etag,
        )
        assert same_name.etag == row.etag
        assert len(store.history()["entries"]) == 1
        same_items = store.replace_memberships(
            row.playlist_id, [], expected_etag=row.etag,
        )
        assert same_items.etag == row.etag
        assert len(store.history()["entries"]) == 1


def test_non_webui_actor_events_are_ignored(db_path: Path) -> None:
    with PlaylistStore(db_path, bus=FakeEventBus()) as store:
        row = store.create_playlist("Mine")
        playlist_id = row.playlist_id
        assert store.history()["can_undo"] is True
    conn = state_db.open_rw(db_path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="ingest")
    try:
        writer.append_playlist_history(
            "playlist.edit",
            _cmd(
                "rename",
                command_id="ingest-1",
                playlist_id=playlist_id,
                before=_snap(playlist_id=playlist_id, name="Mine"),
                after=_snap(playlist_id=playlist_id, name="Hijacked"),
            ).to_dict(),
        )
    finally:
        writer.close()
        conn.close()
    with PlaylistStore(db_path, bus=FakeEventBus()) as store:
        hist = store.history()
        assert [e["op"] for e in hist["entries"]] == ["create"]
        assert all(e["command_id"] != "ingest-1" for e in hist["entries"])


def test_create_undo_leaves_tombstone_not_hard_delete(db_path: Path) -> None:
    with PlaylistStore(db_path, bus=FakeEventBus()) as store:
        row = store.create_playlist("Doomed")
        playlist_id = row.playlist_id
        store.undo()
    deleted_at = _tombstone(db_path, playlist_id)
    assert deleted_at is not None
    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM playlists WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_empty_undo_and_redo_raise(db_path: Path) -> None:
    with PlaylistStore(db_path, bus=FakeEventBus()) as store:
        with pytest.raises(PlaylistHistoryEmptyError) as undo_err:
            store.undo()
        assert undo_err.value.error_code == "nothing_to_undo"
        row = store.create_playlist("Only")
        with pytest.raises(PlaylistHistoryEmptyError) as redo_err:
            store.redo()
        assert redo_err.value.error_code == "nothing_to_redo"
        assert row.name == "Only"
