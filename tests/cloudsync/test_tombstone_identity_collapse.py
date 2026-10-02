"""CLOUDSYNC-31: a removed track never takes part in a content-identity collapse.

Contract: ``docs/decisions/ADR-NEW-deletes-stay-deleted.md``, "Removed and
re-added copies". Every scenario runs real folder ingest of a real WAV, the
real writer's remove, the real hub router and the real ``run_sync``; a sync
that leaves the two sides different raises ``SyncDigestMismatch``.

- [if] a removed track's tombstone reaches the hub after the same audio was added back under a new path
  [then] the removal and the re-add stay two rows on every machine and no remap names the removed one, [else stop].
- [if] the watched-folder rescan then finds the removed file again [then] it stays removed, [else stop].
- [if] the tombstone is NEWER than the re-add [then] the re-add still survives everywhere, [else stop].
- [if] a live copy of a removed track is pushed after the re-add [then] the re-add survives and the copy stays removed, [else stop].
- [if] a spoke is already stuck (removed track live again, held out by a remap the collapse wrote)
  [then] the next sync converges it and the one after pushes nothing, [else stop].
- [if] the hub rejects an offered row [then] the sync result names its table, key and reason, [else stop].

Control for over-correction: two LIVE copies of the same audio on two
machines still collapse to one track.
"""

from __future__ import annotations

import shutil
import sqlite3
import struct
import wave
from pathlib import Path

import pytest

from apps.shared.state import sync_stamp
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import folder, folder_rescan
from apps.shared.state.writer import StateWriter
from apps.sync_hub import client, engine_apply, engine_identity, maintenance
from apps.sync_hub import status as sync_status
from tests.cloudsync.test_hub_sync import _DEV_B, _open, _set_track_title, _sync, _TestClientTransport

pytestmark = pytest.mark.requirement("CLOUDSYNC-31")


# ----- helpers -----------------------------------------------------------


def _write_wav(path: Path, *, sample: int = 7) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<hhhh", sample, sample, sample, sample))


def _ingest(data_dir: Path, root: Path, *, picked_by_user: bool) -> None:
    """Folder ingest; ``picked_by_user`` is the drop / import-modal path (LIBM-141)."""
    conn = _open(data_dir)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="webui")
    try:
        folder.ingest_folder(
            writer, [root], dry_run=False, allow_mass_missing=True, restore_removed=picked_by_user
        )
    finally:
        writer.close()
        conn.close()


def _remove(data_dir: Path, stable_id: str) -> None:
    conn = _open(data_dir)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="webui")
    try:
        writer.remove_from_library(stable_id)
    finally:
        writer.close()
        conn.close()


def _rescan(data_dir: Path, root: Path) -> folder_rescan.FolderRescanReport:
    conn = _open(data_dir)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="folder-rescan")
    try:
        return folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    finally:
        writer.close()
        conn.close()


def _id_at(data_dir: Path, path: Path) -> str:
    conn = _open(data_dir)
    try:
        row = conn.execute("SELECT stable_id FROM tracks WHERE file_path = ?", (str(path),)).fetchone()
    finally:
        conn.close()
    assert row is not None, f"{data_dir.name} holds no track at {path}"
    return str(row[0])


def _removed(data_dir: Path, stable_id: str) -> bool | None:
    """True removed, False live, None when the row is gone altogether."""
    conn = _open(data_dir)
    try:
        row = conn.execute("SELECT deleted_at FROM tracks WHERE stable_id = ?", (stable_id,)).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0] is not None


def _remaps(data_dir: Path) -> dict[str, str]:
    conn = _open(data_dir)
    try:
        return dict(conn.execute("SELECT loser_pk, survivor_pk FROM sync_identity_remap").fetchall())
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def _live_count(data_dir: Path) -> int:
    conn = _open(data_dir)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL").fetchone()[0])
    finally:
        conn.close()


def _location_owner(data_dir: Path, path: Path) -> list[str]:
    conn = _open(data_dir)
    try:
        return sorted(
            str(row[0])
            for row in conn.execute("SELECT stable_id FROM track_locations WHERE file_path = ?", (str(path),))
        )
    finally:
        conn.close()


class _Library:
    """One recording in a watched folder, held by spokes A and B."""

    def __init__(self, tmp_path: Path) -> None:
        self.music = tmp_path / "music"
        self.song = self.music / "song.wav"
        self.inbox = tmp_path / "inbox" / "batch-1"
        _write_wav(self.song)

    def add_back(self, data_dir: Path) -> None:
        """The UI drop: the same bytes staged at a new path, ingested as picked."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.song, self.inbox / "song.wav")
        _ingest(data_dir, self.inbox, picked_by_user=True)


def _shared_library(
    hub: _TestClientTransport, spoke_a: Path, spoke_b: Path, tmp_path: Path
) -> tuple[_Library, str]:
    library = _Library(tmp_path)
    _ingest(spoke_a, library.music, picked_by_user=False)
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    return library, _id_at(spoke_a, library.song)


def _assert_two_rows_everywhere(machines: tuple[Path, ...], removed: str, re_added: str) -> None:
    for machine in machines:
        assert _removed(machine, removed) is True, (
            f"{machine.name}: the removed track is {_removed(machine, removed)!r}, not removed "
            "(None: the collapse hard-deleted it, so a rescan brings it back)"
        )
        assert _removed(machine, re_added) is False, f"{machine.name}: the re-added track is not live"
        assert removed not in _remaps(machine), f"{machine.name}: a remap names the removed track"


# ----- the remove, re-add, sync sequence ------------------------------------


def test_a_removed_track_and_its_re_add_stay_two_rows(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path, tmp_path: Path
) -> None:
    """The order the Mac hit: the tombstone reaches the hub after the re-add.

    B removes the track while offline; A removes it, adds the file back by
    drop and syncs; then B syncs its own, older tombstone. Before the fix the
    hub collapsed B's tombstone into A's re-add, B hard-deleted its removed
    row and moved its location onto the re-add, and the rescan re-inserted
    the file live.
    """
    library, removed = _shared_library(hub, spoke_a, spoke_b, tmp_path)
    _remove(spoke_b, removed)
    _remove(spoke_a, removed)
    _sync(spoke_a, hub, "spoke-a")
    library.add_back(spoke_a)
    _sync(spoke_a, hub, "spoke-a")
    re_added = _id_at(spoke_a, library.inbox / "song.wav")
    assert re_added != removed

    _sync(spoke_b, hub, "spoke-b")

    _assert_two_rows_everywhere((hub_dir, spoke_a, spoke_b), removed, re_added)
    assert _location_owner(spoke_b, library.song) == [removed], (
        "B's location for the removed file moved onto the re-add"
    )

    report = _rescan(spoke_b, library.music)

    assert report.tracks_added == 0, "the watched-folder rescan resurrected a removed track"
    assert _removed(spoke_b, removed) is True
    result = _sync(spoke_b, hub, "spoke-b")
    assert (result.rejected, result.digest_inconclusive) == (0, False)


def test_one_machine_remove_drop_sync_rescan_keeps_the_removal(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, tmp_path: Path
) -> None:
    """The Mac sequence on ONE machine, original file still under a watched root.

    What re-offers the tombstone after the re-add is the push fence. The seed
    collapses a pair of live duplicates of another recording and drops the
    loser here; the rescan re-inserts it (the Mac saw 40 of 152 come back
    this way), it is held as an identity loser, and the fence stays below it,
    so every later sync re-offers everything logged since, the removal
    included. Before the fix that re-offer lost the collapse to the re-add,
    the spoke hard-deleted its removed row, and the rescan re-inserted it live.
    """
    library = _Library(tmp_path)
    twin = library.music / "twin" / "a.wav"
    _write_wav(twin, sample=3)
    shutil.copy2(twin, library.music / "twin" / "b.wav")
    _ingest(spoke_a, library.music, picked_by_user=False)
    _sync(spoke_a, hub, "spoke-a")
    assert _rescan(spoke_a, library.music).tracks_added == 1, "premise: the duplicate loser came back"
    removed = _id_at(spoke_a, library.song)
    _remove(spoke_a, removed)
    _sync(spoke_a, hub, "spoke-a")
    library.add_back(spoke_a)
    re_added = _id_at(spoke_a, library.inbox / "song.wav")

    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_a, hub, "spoke-a")
    _assert_two_rows_everywhere((hub_dir, spoke_a), removed, re_added)
    _rescan(spoke_a, library.music)

    assert _removed(spoke_a, removed) is True, "the watched-folder rescan resurrected a removed track"
    result = _sync(spoke_a, hub, "spoke-a")
    _assert_two_rows_everywhere((hub_dir, spoke_a), removed, re_added)
    # The fence the re-inserted duplicate loser pins still re-offers rows the
    # hub already holds (a separate defect); none of them may be refused as
    # a removal or an identity loss any more.
    refused = [(row.table, row.reason) for row in result.rejected_rows if row.reason != "not_newer"]
    assert refused == [], f"the hub still refuses rows of the pair: {result.rejected_rows}"


def test_a_tombstone_newer_than_the_re_add_does_not_erase_the_re_add(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path, tmp_path: Path
) -> None:
    """The other direction: B removes the track only after A added it back."""
    library, removed = _shared_library(hub, spoke_a, spoke_b, tmp_path)
    _remove(spoke_a, removed)
    _sync(spoke_a, hub, "spoke-a")
    library.add_back(spoke_a)
    _sync(spoke_a, hub, "spoke-a")
    re_added = _id_at(spoke_a, library.inbox / "song.wav")
    _remove(spoke_b, removed)

    _sync(spoke_b, hub, "spoke-b")
    _sync(spoke_a, hub, "spoke-a")

    _assert_two_rows_everywhere((hub_dir, spoke_a, spoke_b), removed, re_added)


def test_a_live_copy_of_a_removed_track_cannot_win_against_the_re_add(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path, tmp_path: Path
) -> None:
    """B never pulled the removal and edits its live copy after the re-add.

    Its ``updated_at`` is the newest of all, so a content-identity collapse
    decided before the lifecycle rule elected it and dropped the re-add.
    """
    library, removed = _shared_library(hub, spoke_a, spoke_b, tmp_path)
    _remove(spoke_a, removed)
    _sync(spoke_a, hub, "spoke-a")
    library.add_back(spoke_a)
    _sync(spoke_a, hub, "spoke-a")
    re_added = _id_at(spoke_a, library.inbox / "song.wav")
    conn = _open(spoke_b)
    try:
        _set_track_title(
            conn, removed, title="edited after the re-add", updated_at=sync_stamp.canonical_now(), origin=_DEV_B
        )
        conn.commit()
    finally:
        conn.close()

    result = maintenance.sync(spoke_b, "http://hub.invalid", transport=hub, name="spoke-b", force=True)
    _sync(spoke_a, hub, "spoke-a")

    _assert_two_rows_everywhere((hub_dir, spoke_a, spoke_b), removed, re_added)
    assert [(row.table, row.pk, row.reason) for row in result.rejected_rows] == [
        ("tracks", (removed,), "removed_on_hub")
    ], "the sync did not name the row the hub rejected"
    journal = sync_status.read_results(spoke_b)[-1]
    assert f"tracks ['{removed}'] (removed_on_hub)" in journal.message, journal.message


# ----- a spoke already stuck in the loop ------------------------------------


def test_a_spoke_stuck_in_the_loop_converges(
    hub: _TestClientTransport,
    hub_dir: Path,
    spoke_a: Path,
    spoke_b: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reach the stuck state with both fixes switched off, then switch them on.

    Stuck: B holds the removed track live again (the rescan re-inserted it),
    B and the hub both hold the remap removed -> re-add the collapse wrote,
    so B keeps the row out of every offer and never meets the hub's
    tombstone; the hub rejects B's moved rows on every sync.
    """
    monkeypatch.setattr(engine_identity, "is_removed", lambda _values: False)
    monkeypatch.setattr(engine_apply, "retire_tombstoned_remap_losers", lambda _conn: ())
    monkeypatch.setattr(engine_apply, "_tombstone_outranks", lambda _lifecycle: False)
    library, removed = _shared_library(hub, spoke_a, spoke_b, tmp_path)
    _remove(spoke_b, removed)
    _remove(spoke_a, removed)
    _sync(spoke_a, hub, "spoke-a")
    library.add_back(spoke_a)
    _sync(spoke_a, hub, "spoke-a")
    re_added = _id_at(spoke_a, library.inbox / "song.wav")
    _sync(spoke_b, hub, "spoke-b")
    assert _removed(spoke_b, removed) is None, "premise: the collapse hard-deleted B's removed row"
    assert _rescan(spoke_b, library.music).tracks_added == 1, "premise: the rescan resurrected it"
    _sync(spoke_b, hub, "spoke-b")
    stuck = _sync(spoke_b, hub, "spoke-b")
    assert stuck.rejected > 0, "premise: B is stuck re-offering rows the hub rejects"
    assert _removed(spoke_b, removed) is False
    assert _remaps(spoke_b) == {removed: re_added} == _remaps(hub_dir)
    monkeypatch.undo()

    _sync(spoke_b, hub, "spoke-b")

    _assert_two_rows_everywhere((hub_dir, spoke_b), removed, re_added)
    assert _live_count(spoke_b) == _live_count(hub_dir) == 1
    _sync(spoke_b, hub, "spoke-b")
    settled = _sync(spoke_b, hub, "spoke-b")
    assert (settled.pushed, settled.rejected, settled.digest_inconclusive) == (0, 0, False), (
        "B still re-offers rows the hub rejects"
    )
    _sync(spoke_a, hub, "spoke-a")
    _assert_two_rows_everywhere((hub_dir, spoke_a, spoke_b), removed, re_added)


# ----- control: genuine duplicates still collapse -----------------------------


def test_two_live_copies_of_the_same_audio_still_collapse(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path, tmp_path: Path
) -> None:
    """Overshoot control: the fix exempts removed rows only, not every duplicate."""
    library = _Library(tmp_path)
    _ingest(spoke_a, library.music, picked_by_user=False)
    other = tmp_path / "elsewhere" / "song.wav"
    other.parent.mkdir(parents=True)
    shutil.copy2(library.song, other)
    _ingest(spoke_b, other.parent, picked_by_user=False)
    first, second = _id_at(spoke_a, library.song), _id_at(spoke_b, other)
    assert first != second

    _sync(spoke_a, hub, "spoke-a")
    result = _sync(spoke_b, hub, "spoke-b")
    _sync(spoke_a, hub, "spoke-a")

    assert isinstance(result, client.SyncResult)
    assert _live_count(hub_dir) == 1, "two live copies of one recording did not collapse"
    collapsed = _remaps(hub_dir)
    assert len(collapsed) == 1
    assert set(next(iter(collapsed.items()))) == {first, second}
    # A later push still finds the remap: only a REMOVED loser's is retired.
    _write_wav(tmp_path / "later" / "other.wav", sample=9)
    _ingest(spoke_a, tmp_path / "later", picked_by_user=False)
    assert _sync(spoke_a, hub, "spoke-a").accepted > 0
    assert _remaps(hub_dir) == collapsed, "a push retired a genuine duplicate's remap"
