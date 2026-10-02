"""A playlist entry naming a content-identity loser follows its survivor.

The Mac check of PR #4930 (Fri 2 Oct 2026): after a removed track is
restored, re-dropping the same file creates a second row for one recording,
the hub collapses one of the two into the other, and since CLOUDSYNC-32 a
loser the hub answered for is SETTLED: never offered. The drop that created
the row also put it in a playlist. The question was whether that entry is
left naming the settled loser (a local-only entry that never reaches the hub
or another machine) or follows the survivor.

It follows the survivor. ``prepare_spoke_identity`` runs before every offer
and rewrites every child of every effective loser, ``playlist_memberships``
included, onto its survivor (``remap_track_children``), dropping the loser's
entry where the playlist already holds the survivor; the hub's
``apply_hub_identity_rejects`` does the same the moment it answers, and a
bundle that reaches the hub before its sender learned the verdict has its
member renamed there by ``rewrite_incoming_change``. No machine is left with
an entry naming a loser, and the playlist travels.

Every scenario runs real folder ingest of real WAVs, the real
``PlaylistStore`` the drop's ``POST /playlists`` and ``items:add`` calls use,
the real hub router and the real ``run_sync``; a sync that leaves the two
sides different raises ``SyncDigestMismatch``.

- [if] a re-drop after a restore lands in a playlist [then] the entry names the survivor on the dropping spoke, hub and second spoke, [else stop].
- [if] the dropped row loses to a newer restore elsewhere [then] the entry follows the restored original everywhere, [else stop].
- [if] an already-settled loser is added to a playlist [then] every machine holds the survivor once, never the loser and never nothing, [else stop].
- [if] the entry was remapped [then] the next sync pushes nothing and holds nothing, [else stop].
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apps.sync_hub import client
from apps.webui.server.playlist_store import PlaylistStore
from tests.cloudsync.test_hub_sync import _open, _sync, _TestClientTransport
from tests.cloudsync.test_settled_identity_losers import _assert_settled
from tests.cloudsync.test_tombstone_identity_collapse import (
    _id_at,
    _ingest,
    _remaps,
    _remove,
    _removed,
    _write_wav,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-32")


class _Drop:
    """A watched folder holding one song, and the inbox batch the UI drop stages into."""

    def __init__(self, tmp_path: Path) -> None:
        self.music = tmp_path / "music"
        self.song = self.music / "song.wav"
        self.inbox = tmp_path / "inbox" / "batch-2"
        _write_wav(self.song, sample=11)

    def stage(self, data_dir: Path) -> str:
        """Stage the song's bytes in the inbox and run what materialize runs; the staged row's id."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        staged = self.inbox / "song.wav"
        shutil.copy2(self.song, staged)
        _ingest(data_dir, self.inbox, picked_by_user=True)
        return _id_at(data_dir, staged)


def _new_playlist(data_dir: Path, name: str, stable_ids: list[str]) -> str:
    """``POST /playlists`` then ``items:add``, as the playlist-folder drop calls them."""
    store = PlaylistStore(client.state_db_path(data_dir))
    try:
        playlist_id = store.create_playlist(name).playlist_id
        store.add_memberships(playlist_id, stable_ids)
    finally:
        store.close()
    return playlist_id


def _add(data_dir: Path, playlist_id: str, stable_ids: list[str]) -> None:
    store = PlaylistStore(client.state_db_path(data_dir))
    try:
        store.add_memberships(playlist_id, stable_ids)
    finally:
        store.close()


def _members(data_dir: Path, playlist_id: str) -> list[str] | None:
    """The playlist's member stable_ids in order, or None when the playlist is absent."""
    conn = _open(data_dir)
    try:
        if conn.execute("SELECT 1 FROM playlists WHERE playlist_id = ?", (playlist_id,)).fetchone() is None:
            return None
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT stable_id FROM playlist_memberships WHERE playlist_id = ? ORDER BY order_key, item_id",
                (playlist_id,),
            )
        ]
    finally:
        conn.close()


def _assert_everywhere(machines: tuple[Path, ...], playlist_id: str, expected: list[str]) -> None:
    for machine in machines:
        members = _members(machine, playlist_id)
        assert members == expected, (
            f"{machine.name}: playlist {playlist_id} holds {members!r}, expected {expected!r} "
            "(None: the playlist never reached this machine)"
        )


def _assert_quiet(*machines: tuple[Path, str], hub: _TestClientTransport) -> None:
    for data_dir, name in machines:
        _assert_settled(_sync(data_dir, hub, name), f"{name} after the remap")


# ----- the Mac sequence: restore, then re-drop into a playlist ---------------


@pytest.mark.parametrize("with_original", [False, True], ids=["copy-only", "original-and-copy"])
def test_a_re_drop_after_a_restore_lands_on_the_survivor_everywhere(
    hub: _TestClientTransport,
    hub_dir: Path,
    spoke_a: Path,
    spoke_b: Path,
    tmp_path: Path,
    with_original: bool,
) -> None:
    drop = _Drop(tmp_path)
    _ingest(spoke_a, drop.music, picked_by_user=False)
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    original = _id_at(spoke_a, drop.song)
    _remove(spoke_a, original)
    _sync(spoke_a, hub, "spoke-a")
    _ingest(spoke_a, drop.music, picked_by_user=True)
    _sync(spoke_a, hub, "spoke-a")
    assert _removed(spoke_a, original) is False, "premise: the picked re-import restored the track"
    dropped = drop.stage(spoke_a)
    assert dropped != original, "premise: the re-drop is a second row for the same recording"
    playlist_id = _new_playlist(spoke_a, "dropped", [original, dropped] if with_original else [dropped])

    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    remaps = _remaps(spoke_a)
    assert remaps.get(dropped) == original or remaps.get(original) == dropped, (
        f"premise: the hub collapsed the two rows: {remaps}"
    )
    survivor = original if remaps.get(dropped) == original else dropped
    _assert_everywhere((spoke_a, hub_dir, spoke_b), playlist_id, [survivor])
    _assert_quiet((spoke_a, "spoke-a"), (spoke_b, "spoke-b"), hub=hub)


def test_a_drop_that_loses_to_a_newer_restore_follows_the_original(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path, tmp_path: Path
) -> None:
    """B drops the file into a playlist; A restores the original afterwards and syncs first.

    The hub keeps A's newer restore, so B's dropped row is the loser the hub
    answers for, and B's playlist entry must not stay on it.
    """
    drop = _Drop(tmp_path)
    _ingest(spoke_a, drop.music, picked_by_user=False)
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    original = _id_at(spoke_a, drop.song)
    _remove(spoke_a, original)
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    dropped = drop.stage(spoke_b)
    playlist_id = _new_playlist(spoke_b, "dropped on b", [dropped])
    _ingest(spoke_a, drop.music, picked_by_user=True)
    _sync(spoke_a, hub, "spoke-a")

    _sync(spoke_b, hub, "spoke-b")
    _sync(spoke_a, hub, "spoke-a")

    assert _remaps(spoke_b).get(dropped) == original, f"premise: the drop lost: {_remaps(spoke_b)}"
    _assert_everywhere((spoke_b, hub_dir, spoke_a), playlist_id, [original])
    _assert_quiet((spoke_b, "spoke-b"), (spoke_a, "spoke-a"), hub=hub)


# ----- the generic case: a loser that is ALREADY settled -------------------


@pytest.mark.parametrize("holds_survivor", [True, False], ids=["playlist-holding-survivor", "new-playlist"])
def test_an_already_settled_loser_added_to_a_playlist_lands_once_on_the_survivor(
    hub: _TestClientTransport,
    hub_dir: Path,
    spoke_a: Path,
    spoke_b: Path,
    tmp_path: Path,
    holds_survivor: bool,
) -> None:
    """The re-drop staged at a path whose row the hub already collapsed.

    After the collapse in the scenario above, B re-drops into the same batch:
    the staged row comes back under its old id, which the persisted hub remap
    names a loser, so it is settled the moment it exists. B adds it either to
    a playlist every machine already holds with the survivor in it, or to a
    new playlist. The entry is rewritten onto the survivor before the bundle
    is ever offered: where the survivor is already there the loser's entry
    goes (one copy of the track), and where it is not the entry is kept and
    renamed, never dropped.
    """
    drop = _Drop(tmp_path)
    _ingest(spoke_a, drop.music, picked_by_user=False)
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    original = _id_at(spoke_a, drop.song)
    _remove(spoke_a, original)
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    dropped = drop.stage(spoke_b)
    _ingest(spoke_a, drop.music, picked_by_user=True)
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    assert _removed(spoke_b, dropped) is None, "premise: the collapse dropped B's losing row"
    if holds_survivor:
        playlist_id = _new_playlist(spoke_a, "set list", [original])
        _sync(spoke_a, hub, "spoke-a")
        _sync(spoke_b, hub, "spoke-b")
        _assert_everywhere((spoke_a, hub_dir, spoke_b), playlist_id, [original])

    assert drop.stage(spoke_b) == dropped, "premise: the re-drop re-inserted the settled loser"
    assert _remaps(spoke_b).get(dropped) == original, "premise: a persisted hub remap names it a loser"
    if holds_survivor:
        _add(spoke_b, playlist_id, [dropped])
        assert _members(spoke_b, playlist_id) == [original, dropped], "premise: the add named the loser"
    else:
        playlist_id = _new_playlist(spoke_b, "dropped again", [dropped])
        assert _members(spoke_b, playlist_id) == [dropped], "premise: the add named the loser"

    _sync(spoke_b, hub, "spoke-b")
    _sync(spoke_a, hub, "spoke-a")

    _assert_everywhere((spoke_b, hub_dir, spoke_a), playlist_id, [original])
    _assert_quiet((spoke_b, "spoke-b"), (spoke_a, "spoke-a"), hub=hub)
