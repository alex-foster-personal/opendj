"""Adding to a playlist does work independent of its size (LIBM-132, #3963).

[if] an add's work grows with the playlist's membership [then] fail, [else stop].

Found at 10k members (LIBM-120 measurement, Fri 25 Sep 2026): one
``items:add`` cost 634 ms against 5.8 ms at 50 members. #4006 (LIBM-131) cut
three full membership reads to one; that one read, the response ``items`` and
the undo snapshots still scaled with the playlist (246 ms at 10,042 members).
LIBM-132 removes all of them: bounded neighbor and duplicate reads on the
v21 indexes, a response of the inserted rows only, and an ``add_items`` undo
command that records those rows instead of two membership snapshots.

Instruments, all on the store's REAL connection, none a mock:

* sqlite VM instructions (``set_progress_handler(cb, 1)``) - catches C-level
  scans and sorts that no Python counter sees. A full-table walk or a
  ``USE TEMP B-TREE`` sort costs instructions per row; an index seek does not.
* rows handed to Python (:class:`RowCounter`).
* the ``playlist.edit`` payload's size.
* ``EXPLAIN QUERY PLAN`` of every membership SELECT the add really ran.

Legacy order_keys come from a checksum-locked dump of a database the
pre-LIBM-132 build wrote (``tests/fixtures/playlist-legacy-order-keys``),
restored to a disposable copy and opened through the production migration.

Regression one-liners:
  - if an append or head insert runs more sqlite steps at 4,000 members than at 40 then broken
  - if an add materializes more rows at 4,000 members than at 40 then broken
  - if the add_items history payload grows with the membership then broken
  - if any membership read of an add scans the table or sorts in a temp b-tree then broken
  - if a positioned add lands anywhere but the requested index then broken
  - if a positioned add to a legacy NULL-key playlist lands anywhere but the requested index then broken
  - if undo of an add removes anything but the rows it inserted then broken
  - if redo of an add restores different rows or order_keys than undo removed then broken
  - if undo proceeds after an added row was already removed then broken
  - if a peer write can land between the neighbor read and the insert then broken
  - if an appended key is longer than the key before it then broken
  - if an add before an empty, beside an overlong, or between two shared legacy keys fails
    or misplaces the row then broken
  - if a move before an empty legacy key lands anywhere but first then broken
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state.order_key import MAX_ORDER_KEY_LEN
from apps.shared.state.schema import SCHEMA_VERSION
from apps.webui.server.backend import BackendError, ConflictError
from apps.webui.server.playlist_add import MEMBERSHIP_ORDER_BY, _load_live_members
from apps.webui.server.playlist_store import PlaylistStore
from tests.webui.sql_trace import RowCounter

pytestmark = pytest.mark.requirement("LIBM-132")

SMALL, LARGE = 40, 4000
NOW = "2026-09-29T00:00:00Z"


def _sid(i: int) -> str:
    return f"{i:040x}"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[PlaylistStore]:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    conn.executemany(
        "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, file_path, "
        "created_at, updated_at) VALUES (?, 'inferred', 1000, NULL, ?, ?)",
        [(_sid(i), NOW, NOW) for i in range(LARGE + 10)],
    )
    conn.commit()
    conn.close()
    playlist_store = PlaylistStore(path)  # as routes/playlist_write.py builds it
    yield playlist_store
    playlist_store.close()


def _playlist(store: PlaylistStore, size: int, *, forbid_duplicates: bool = False) -> str:
    row = store.create_playlist(f"{size} members")
    row = store.replace_memberships(
        row.playlist_id,
        [_sid(i) for i in range(size)],
        expected_etag=row.etag,
    )
    if forbid_duplicates:
        store.update_playlist(row.playlist_id, expected_etag=row.etag, forbid_duplicates=True)
    return row.playlist_id


def _live_stable_ids(store: PlaylistStore, playlist_id: str) -> list[str]:
    return [m.stable_id for m in _load_live_members(store._conn, playlist_id)]


def _vm_steps(store: PlaylistStore, action: Callable[[], object]) -> int:
    steps = [0]

    def _tick() -> int:
        steps[0] += 1
        return 0

    store._conn.set_progress_handler(_tick, 1)
    try:
        action()
    finally:
        store._conn.set_progress_handler(None, 1)
    return steps[0]


def _rows_materialized(store: PlaylistStore, action: Callable[[], object]) -> int:
    counter = RowCounter()
    store._conn.row_factory = counter
    try:
        action()
    finally:
        store._conn.row_factory = sqlite3.Row
    return counter.rows


def _last_edit_payload(store: PlaylistStore) -> dict:
    row = store._conn.execute(
        "SELECT payload_json FROM events WHERE kind = 'playlist.edit' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return json.loads(row[0])


# ---------------------------------------------------------------------------
# growth


@dataclass(frozen=True)
class AddCase:
    position: int | None
    forbid_duplicates: bool


CASES = {
    "append": AddCase(position=None, forbid_duplicates=False),
    "head": AddCase(position=0, forbid_duplicates=False),
    "append-forbid-duplicates": AddCase(position=None, forbid_duplicates=True),
}


@pytest.mark.parametrize("case", list(CASES))
def test_add_executes_the_same_sqlite_work_at_4000_members_as_at_40(
    store: PlaylistStore, case: str,
) -> None:
    """[if] one add at 4,000 members [then] sqlite does the work it does at 40, [else stop]."""
    spec = CASES[case]
    small = _playlist(store, SMALL, forbid_duplicates=spec.forbid_duplicates)
    large = _playlist(store, LARGE, forbid_duplicates=spec.forbid_duplicates)

    def _add(playlist_id: str, sid: str) -> Callable[[], object]:
        return lambda: store.add_memberships(playlist_id, [sid], position=spec.position)

    small_steps = _vm_steps(store, _add(small, _sid(LARGE + 1)))
    large_steps = _vm_steps(store, _add(large, _sid(LARGE + 1)))
    # Positive control: the handler really counts, and a full read of the
    # large playlist costs thousands of instructions, so a scan inside the
    # add could not hide under the tolerance below.
    full_read_steps = _vm_steps(store, lambda: _load_live_members(store._conn, large))
    assert small_steps > 100, "progress handler saw no instructions: not attached"
    assert full_read_steps > 10 * LARGE, full_read_steps
    # B-tree depth grows by at most one level from 40 to 4,000 rows; that is
    # a handful of instructions, never one per member.
    assert large_steps - small_steps < 64, (
        f"{case}: {small_steps} sqlite instructions at {SMALL} members, "
        f"{large_steps} at {LARGE}; a full read costs {full_read_steps}"
    )


@pytest.mark.parametrize("case", list(CASES))
def test_add_materializes_the_same_rows_at_4000_members_as_at_40(
    store: PlaylistStore, case: str,
) -> None:
    """[if] one add at 4,000 members [then] Python sees as many rows as at 40, [else stop]."""
    spec = CASES[case]
    small = _playlist(store, SMALL, forbid_duplicates=spec.forbid_duplicates)
    large = _playlist(store, LARGE, forbid_duplicates=spec.forbid_duplicates)
    small_rows = _rows_materialized(
        store, lambda: store.add_memberships(small, [_sid(LARGE + 2)], position=spec.position),
    )
    large_rows = _rows_materialized(
        store, lambda: store.add_memberships(large, [_sid(LARGE + 2)], position=spec.position),
    )
    assert small_rows > 0, "counter saw no rows: the instrument is not attached"
    assert large_rows == small_rows, (small_rows, large_rows)


def test_positioned_add_materializes_at_most_the_two_neighbors(store: PlaylistStore) -> None:
    """[if] a position is given [then] only the neighbors' order_keys are read, [else stop]."""
    large = _playlist(store, LARGE)
    head_rows = _rows_materialized(
        store, lambda: store.add_memberships(large, [_sid(LARGE + 3)], position=0),
    )
    middle_rows = _rows_materialized(
        store, lambda: store.add_memberships(large, [_sid(LARGE + 3)], position=LARGE // 2),
    )
    # head reads one neighbor, middle reads two.
    assert middle_rows == head_rows + 1, (head_rows, middle_rows)


def test_add_items_history_payload_does_not_grow_with_the_playlist(store: PlaylistStore) -> None:
    """[if] an add is recorded [then] its payload holds the inserted rows only, [else stop]."""
    sizes: dict[int, int] = {}
    for size in (SMALL, LARGE):
        playlist_id = _playlist(store, size)
        result = store.add_memberships(playlist_id, [_sid(LARGE + 4), _sid(1)])
        payload = _last_edit_payload(store)
        assert payload["op"] == "add_items"
        assert payload["added"] == [m.to_dict() for m in result.added]
        assert payload["before"] is None
        assert payload["after"]["items"] == []
        sizes[size] = len(json.dumps(payload["added"]))
    assert sizes[SMALL] == sizes[LARGE], sizes


def test_every_membership_read_of_an_add_seeks_an_index(store: PlaylistStore) -> None:
    """[if] an add runs [then] no membership read scans or sorts in a temp b-tree, [else stop]."""
    playlist_id = _playlist(store, 200, forbid_duplicates=True)
    statements: list[str] = []
    for offset, position in enumerate((None, 0, 100, 200)):
        store._conn.set_trace_callback(statements.append)
        try:
            store.add_memberships(playlist_id, [_sid(LARGE + 5 + offset)], position=position)
        finally:
            store._conn.set_trace_callback(None)
    reads = [
        s for s in statements
        if s.lstrip().upper().startswith("SELECT") and "FROM playlist_memberships" in s
    ]
    # Positive control: the duplicate check, the append read, the head read
    # and the positioned read all ran.
    assert any("stable_id IN" in s for s in reads), reads
    assert any(" DESC " in s for s in reads), reads
    assert any(" OFFSET " in s for s in reads), reads
    for sql in reads:
        plan = " | ".join(
            row[3] for row in store._conn.execute(f"EXPLAIN QUERY PLAN {sql}")
        )
        assert plan.startswith("SEARCH playlist_memberships"), (sql, plan)
        if "ORDER BY" in sql or "stable_id IN" in sql:
            assert "idx_playlist_memberships_live_" in plan, (sql, plan)
        assert "TEMP B-TREE" not in plan, (sql, plan)


# ---------------------------------------------------------------------------
# correctness


def test_positioned_add_lands_at_every_index(store: PlaylistStore) -> None:
    """[if] a position is given [then] the track lands exactly there, [else stop]."""
    playlist_id = _playlist(store, 12)
    store.remove_memberships(playlist_id, [_load_live_members(store._conn, playlist_id)[3].item_id])
    for index in range(len(_live_stable_ids(store, playlist_id)) + 1):
        before = _live_stable_ids(store, playlist_id)
        new_sid = _sid(LARGE + 9)
        result = store.add_memberships(playlist_id, [new_sid], position=index)
        assert [m.stable_id for m in result.added] == [new_sid]
        assert _live_stable_ids(store, playlist_id) == [*before[:index], new_sid, *before[index:]]
        store.remove_memberships(playlist_id, [result.added[0].item_id])


def test_position_past_the_end_is_refused_without_writing(store: PlaylistStore) -> None:
    """[if] position exceeds the member count [then] the add writes nothing, [else stop]."""
    playlist_id = _playlist(store, 3)
    before = _live_stable_ids(store, playlist_id)
    refusal = "position out of range: 5 \\(playlist has 3 live members\\)"
    with pytest.raises(BackendError, match=refusal):
        store.add_memberships(playlist_id, [_sid(LARGE + 1)], position=5)
    assert _live_stable_ids(store, playlist_id) == before
    # Control: position == count is a legal append.
    store.add_memberships(playlist_id, [_sid(LARGE + 1)], position=3)
    assert _live_stable_ids(store, playlist_id) == [*before, _sid(LARGE + 1)]


def test_undo_removes_only_the_added_rows_and_redo_restores_them(store: PlaylistStore) -> None:
    """[if] an add is undone and redone [then] exactly its rows go and come back, [else stop]."""
    playlist_id = _playlist(store, 6)
    # A second copy of member 2 is added: undo must not touch the original.
    result = store.add_memberships(playlist_id, [_sid(2), _sid(LARGE + 1)], position=4)
    added_ids = {m.item_id for m in result.added}
    with_add = _load_live_members(store._conn, playlist_id)

    _command, current = store.undo()
    after_undo = _load_live_members(store._conn, playlist_id)
    assert [m.stable_id for m in after_undo] == [_sid(i) for i in range(6)]
    assert added_ids.isdisjoint(m.item_id for m in after_undo)
    assert current is not None and current.items == [m.stable_id for m in after_undo]

    store.redo()
    after_redo = _load_live_members(store._conn, playlist_id)
    assert [(m.item_id, m.stable_id, m.order_key) for m in after_redo] == [
        (m.item_id, m.stable_id, m.order_key) for m in with_add
    ]


def test_undo_survives_an_unrecorded_write_to_other_members(store: PlaylistStore) -> None:
    """[if] another member changes outside history [then] undo still works, [else stop]."""
    playlist_id = _playlist(store, 5)
    result = store.add_memberships(playlist_id, [_sid(LARGE + 1)])
    # remove_memberships(record_edit=False) is how sync and transfer write.
    store.remove_memberships(
        playlist_id, [_load_live_members(store._conn, playlist_id)[0].item_id], record_edit=False,
    )
    store.undo()
    live = _load_live_members(store._conn, playlist_id)
    assert result.added[0].item_id not in {m.item_id for m in live}
    assert [m.stable_id for m in live] == [_sid(i) for i in range(1, 5)]


def test_undo_refuses_when_an_added_row_is_already_gone(store: PlaylistStore) -> None:
    """[if] an added row is already gone [then] undo conflicts and writes nothing, [else stop]."""
    playlist_id = _playlist(store, 5)
    result = store.add_memberships(playlist_id, [_sid(LARGE + 1), _sid(LARGE + 2)])
    store.remove_memberships(playlist_id, [result.added[0].item_id], record_edit=False)
    before = [(m.item_id, m.order_key) for m in _load_live_members(store._conn, playlist_id)]
    with pytest.raises(ConflictError):
        store.undo()
    after = [(m.item_id, m.order_key) for m in _load_live_members(store._conn, playlist_id)]
    assert after == before
    history = store.history()
    assert history["cursor"] == len(history["entries"]), "cursor moved on a refused undo"


def test_redo_refuses_when_an_added_row_is_live_again(store: PlaylistStore) -> None:
    """[if] an undone add's row came back outside history [then] redo is a conflict, [else stop]."""
    playlist_id = _playlist(store, 5)
    result = store.add_memberships(playlist_id, [_sid(LARGE + 1)])
    store.undo()
    store._writer.restore_playlist_memberships(playlist_id, [result.added[0].item_id])
    with pytest.raises(ConflictError):
        store.redo()


def test_add_items_command_survives_a_store_restart(store: PlaylistStore, tmp_path: Path) -> None:
    """[if] the store is rebuilt from events [then] an add is still undoable, [else stop]."""
    playlist_id = _playlist(store, 5)
    result = store.add_memberships(playlist_id, [_sid(LARGE + 1)])
    store.close()
    reopened = PlaylistStore(tmp_path / "state.db")
    try:
        entry = reopened.history()["entries"][-1]
        assert entry["op"] == "add_items"
        assert entry["label"] == "Add 1 track(s) to '5 members'"
        reopened.undo()
        live = {m.item_id for m in _load_live_members(reopened._conn, playlist_id)}
        assert result.added[0].item_id not in live
    finally:
        reopened.close()


def test_a_concurrent_write_cannot_land_between_the_neighbor_read_and_the_insert(
    store: PlaylistStore,
    tmp_path: Path,
) -> None:
    """[if] a peer writes during an add's neighbor read [then] it is locked out, [else stop]."""
    playlist_id = _playlist(store, 12)
    last_item = _load_live_members(store._conn, playlist_id)[-1].item_id
    peer = sqlite3.connect(str(tmp_path / "state.db"), timeout=0.1, isolation_level=None)
    peer_outcomes: list[str] = []

    def _peer_deletes_the_last_member(statement: str) -> None:
        if f"ORDER BY {MEMBERSHIP_ORDER_BY} LIMIT 2 OFFSET" not in statement or peer_outcomes:
            return
        try:
            peer.execute(
                "UPDATE playlist_memberships SET deleted_at = ? WHERE item_id = ?",
                (NOW, last_item),
            )
            peer_outcomes.append("landed")
        except sqlite3.OperationalError as exc:
            peer_outcomes.append(f"refused: {exc}")

    store._conn.set_trace_callback(_peer_deletes_the_last_member)
    try:
        store.add_memberships(playlist_id, [_sid(LARGE + 3)], position=11)
    finally:
        store._conn.set_trace_callback(None)
        peer.close()

    assert len(peer_outcomes) == 1, "the neighbor read never ran: the probe is not attached"
    assert peer_outcomes[0].startswith("refused: database is locked"), peer_outcomes
    members = _live_stable_ids(store, playlist_id)
    assert members[10:] == [_sid(10), _sid(LARGE + 3), _sid(11)]


# ---------------------------------------------------------------------------
# order_key space: bounded keys, and renumber instead of failing when a gap is empty


def _raw_keys(store: PlaylistStore, playlist_id: str) -> list[str | None]:
    return [
        row[0]
        for row in store._conn.execute(
            f"SELECT order_key FROM playlist_memberships WHERE playlist_id = ? "
            f"AND deleted_at IS NULL ORDER BY {MEMBERSHIP_ORDER_BY}",
            (playlist_id,),
        )
    ]


def test_appended_keys_stay_as_short_as_the_last_key(store: PlaylistStore) -> None:
    """[if] 200 tracks are appended one by one [then] every key stays 8 chars, [else stop]."""
    playlist_id = _playlist(store, SMALL)
    for i in range(200):
        store.add_memberships(playlist_id, [_sid(i % SMALL)])
    keys = _raw_keys(store, playlist_id)
    assert len(keys) == SMALL + 200
    assert {len(k or "") for k in keys} == {8}, sorted({len(k or "") for k in keys})


# ---------------------------------------------------------------------------
# legacy keys: a database the pre-LIBM-132 build really wrote, opened through
# the production migration path. capture.py beside the dump says how each
# playlist got its keys; nothing below writes an order_key itself.

_LEGACY_DIR = Path(__file__).parent / "fixtures" / "playlist-legacy-order-keys"
_LEGACY_DUMP = "v20-legacy-order-keys.sql"
_LEGACY_SCHEMA_VERSION = 20


def _verified_legacy_dump() -> str:
    """The pinned dump, after its manifest version and sha256 both match."""
    manifest = json.loads((_LEGACY_DIR / "manifest.json").read_text())
    assert manifest["version"] == 1, f"unknown manifest version {manifest['version']!r}"
    entry = manifest["files"][_LEGACY_DUMP]
    raw = (_LEGACY_DIR / _LEGACY_DUMP).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == entry["sha256"], (
        f"{_LEGACY_DUMP} does not match its manifest checksum; it is IMMUTABLE, "
        f"expected {entry['sha256']}, got {digest}"
    )
    return raw.decode("utf-8")


@pytest.fixture
def legacy_store(tmp_path: Path) -> Iterator[PlaylistStore]:
    """A disposable copy of the legacy database, opened as the webui opens it."""
    path = tmp_path / "legacy" / "state" / "state.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_verified_legacy_dump())
        restored = conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
    finally:
        conn.close()
    assert restored == _LEGACY_SCHEMA_VERSION, restored
    playlist_store = PlaylistStore(path)  # migrates v20 -> current on open
    migrated = playlist_store._conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
    assert migrated == SCHEMA_VERSION, "the production open did not migrate the legacy db"
    yield playlist_store
    playlist_store.close()


def _legacy_playlist(store: PlaylistStore, name: str) -> str:
    return store._conn.execute(
        "SELECT playlist_id FROM playlists WHERE vendor = 'webui' AND name = ?", (name,),
    ).fetchone()[0]


def _legacy_track_ids(store: PlaylistStore) -> list[str]:
    """The 12 tracks the Spotify import wrote, in Spotify playlist order."""
    spotify_id = store._conn.execute(
        "SELECT playlist_id FROM playlists WHERE vendor = 'spotify'",
    ).fetchone()[0]
    return [
        row[0]
        for row in store._conn.execute(
            "SELECT stable_id FROM playlist_memberships WHERE playlist_id = ? ORDER BY position",
            (spotify_id,),
        )
    ]


def _assert_renumbered(store: PlaylistStore, playlist_id: str) -> list[str]:
    raw = _raw_keys(store, playlist_id)
    keys = [k for k in raw if k is not None]
    assert len(keys) == len(raw) and all(len(k) == 8 for k in keys), raw
    assert keys == sorted(keys) and len(set(keys)) == len(keys), keys
    return keys


def test_positioned_add_lands_at_every_index_of_a_legacy_null_key_playlist(
    legacy_store: PlaylistStore,
) -> None:
    """[if] a legacy NULL-key playlist gets a positioned add [then] it lands there, [else stop]."""
    playlist_id = _legacy_playlist(legacy_store, "Imported from Spotify")
    raw = _raw_keys(legacy_store, playlist_id)
    # Control: Spotify's 12 NULL-key rows with the two live webui adds between them.
    assert raw.count(None) == 12 and len(raw) == 14, raw
    new_sid = _legacy_track_ids(legacy_store)[11]
    # Highest index first: the head insert renumbers, so it runs last.
    for index in range(len(raw), -1, -1):
        before = _live_stable_ids(legacy_store, playlist_id)
        result = legacy_store.add_memberships(playlist_id, [new_sid], position=index)
        assert _live_stable_ids(legacy_store, playlist_id) == [
            *before[:index], new_sid, *before[index:],
        ], index
        legacy_store.remove_memberships(playlist_id, [result.added[0].item_id])
    _assert_renumbered(legacy_store, playlist_id)


@pytest.mark.parametrize(
    ("playlist", "position"),
    [("Head inserts", 0), ("Head inserts", 6), ("Batch append", None)],
    ids=["before-the-empty-key", "between-two-shared-keys", "after-the-overlong-key"],
)
def test_an_add_next_to_a_legacy_key_renumbers_and_lands_in_place(
    legacy_store: PlaylistStore, playlist: str, position: int | None,
) -> None:
    """[if] no key fits beside a legacy key [then] the add renumbers and lands, [else stop]."""
    playlist_id = _legacy_playlist(legacy_store, playlist)
    raw = _raw_keys(legacy_store, playlist_id)
    # Controls: the shapes the legacy add path wrote are really there.
    if playlist == "Head inserts":
        assert raw[0] == "" and raw[-2] == raw[-1] == "00000004", raw
    elif playlist == "Batch append":
        assert len(raw[-1] or "") > MAX_ORDER_KEY_LEN, raw[-1]
    new_sid = _legacy_track_ids(legacy_store)[11]
    before = _live_stable_ids(legacy_store, playlist_id)
    index = len(before) if position is None else position

    result = legacy_store.add_memberships(playlist_id, [new_sid], position=position)

    assert _live_stable_ids(legacy_store, playlist_id) == [*before[:index], new_sid, *before[index:]]
    keys = _assert_renumbered(legacy_store, playlist_id)
    assert result.added[0].order_key == keys[index]


def test_a_move_before_an_empty_legacy_key_lands_first(legacy_store: PlaylistStore) -> None:
    """[if] a row moves before an empty-keyed first row [then] it lands first, [else stop]."""
    playlist_id = _legacy_playlist(legacy_store, "Head inserts")
    assert _raw_keys(legacy_store, playlist_id)[0] == "", "the legacy empty key is not first"
    members = _load_live_members(legacy_store._conn, playlist_id)
    before = [m.stable_id for m in members]
    legacy_store.move_memberships(
        playlist_id,
        range_start=members[-1].item_id,
        range_length=1,
        before_item_id=members[0].item_id,
        expected_etag=legacy_store._load(playlist_id).etag,
    )
    assert _live_stable_ids(legacy_store, playlist_id) == [before[-1], *before[:-1]]
