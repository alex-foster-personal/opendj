"""LIBM-128: continuous re-scan reconciliation over real files and a real DB.

Contract: ``apps/shared/state/ingest/folder_rescan.py``. No mocks: every test
writes real audio bytes to a real ``tmp_path`` folder and reconciles against
a real migrated ``state.db`` connection.

  - [if] a new file appears under a configured root [then] it is a live
    track after one reconcile, [else stop].
  - [if] a file disappears from a configured root [then] its track is
    tombstoned after one reconcile, [else stop].
  - [if] nothing on disk changed since the last cycle [then] the cycle
    writes to no table, [else stop].
  - [if] a root becomes fully denied [then] every cycle warns and none
    silently tombstones it, [else stop].
  - [if] two path spellings of one unchanged file normalize to the same
    key [then] neither is flagged added or removed, [else stop].
"""
from __future__ import annotations

import os
import struct
import unicodedata
import wave
from pathlib import Path
from unittest import mock

import pytest

from apps.shared import fs_access
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import folder_rescan
from apps.shared.state.writer import StateWriter


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<h", 0))


def _live_tracks(state_conn) -> list[tuple[str, str]]:
    rows = state_conn.execute(
        "SELECT stable_id, file_path FROM tracks WHERE deleted_at IS NULL ORDER BY file_path"
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _writer(state_conn) -> StateWriter:
    return StateWriter(state_conn, bus=FakeEventBus(), actor="folder-rescan-test")


# ----- additions and cheap-skip -------------------------------------------


def test_new_file_becomes_a_live_track_after_one_cycle(state_conn, tmp_path: Path) -> None:
    root = tmp_path / "music"
    _write_wav(root / "a.wav")
    writer = _writer(state_conn)

    report = folder_rescan.reconcile_folders(writer, [root], previous_signature="")

    assert report.skipped_no_changes is False
    assert report.tracks_added == 1
    assert report.warning is None
    assert len(_live_tracks(state_conn)) == 1


def test_second_cycle_with_no_disk_change_writes_nothing(state_conn, tmp_path: Path) -> None:
    """[if] a scan cycle finds zero changes [then] it touches no table."""
    root = tmp_path / "music"
    _write_wav(root / "a.wav")
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")

    with (
        mock.patch.object(StateWriter, "upsert_track", side_effect=AssertionError("wrote")),
        mock.patch.object(
            StateWriter, "remove_from_library", side_effect=AssertionError("tombstoned")
        ),
    ):
        second = folder_rescan.reconcile_folders(
            writer, [root], previous_signature=first.signature
        )

    assert second.skipped_no_changes is True
    assert second.signature == first.signature


def test_cheap_skip_survives_a_genuinely_empty_folder() -> None:
    """An empty-but-valid baseline must compare equal by value, never by truthiness."""
    empty_signature = folder_rescan.compute_signature([], [])
    assert empty_signature != ""
    assert bool(empty_signature) is True  # a falsy empty-signature bug would break this


# ----- removal ---------------------------------------------------------------


def test_removed_file_is_tombstoned_next_cycle(state_conn, tmp_path: Path) -> None:
    """A second, untouched file keeps this below LIBM-41's drop threshold, so
    this test exercises plain removal in isolation from the mass-missing
    guard (covered separately below)."""
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav(audio_path)
    _write_wav(root / "keep.wav")
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    assert len(_live_tracks(state_conn)) == 2

    audio_path.unlink()
    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)

    assert second.warning is None
    assert second.tracks_removed == 1
    assert [path for _sid, path in _live_tracks(state_conn)] == [str(root / "keep.wav")]


def test_content_change_at_same_path_keeps_the_one_live_row(
    state_conn, tmp_path: Path
) -> None:
    """[if] a file is rewritten in place [then] the same row stays the only live one, [else stop]."""
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav(audio_path)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    old_id = _live_tracks(state_conn)[0][0]

    _write_wav(audio_path)  # rewritten: new mtime, same path
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)

    live = _live_tracks(state_conn)
    assert len(live) == 1, "a content change must never leave two live rows at one path"
    assert live[0][0] == old_id
    assert live[0][1] == str(audio_path)
    assert second.tracks_removed == 0
    assert second.tracks_added == 0


def test_a_rewrite_with_new_bytes_is_not_reported_as_an_added_track(
    state_conn, tmp_path: Path
) -> None:
    """[if] an existing row is refreshed with new bytes [then] tracks_added stays 0, [else stop]."""
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav(audio_path)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    assert first.tracks_added == 1
    old_hash = state_conn.execute("SELECT audio_hash FROM tracks").fetchone()[0]

    _write_wav_sample(audio_path, 1234)  # new content, same path
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)

    new_hash = state_conn.execute("SELECT audio_hash FROM tracks").fetchone()[0]
    assert new_hash != old_hash, "control: the refresh really changed the stored row"
    assert len(_live_tracks(state_conn)) == 1
    assert second.tracks_added == 0, "a refreshed existing row is not a new track"


# ----- unreadable root: fail loudly, cheaply --------------------------------


def test_denied_root_warns_every_cycle_and_never_mass_tombstones(
    state_conn, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav(audio_path)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    assert len(_live_tracks(state_conn)) == 1

    denied_probe = fs_access.AccessProbe(
        path=str(root), exists=True, readable=False, denied=True, detail="TCC denied (test)", pending=False
    )
    with mock.patch("apps.shared.fs_access.probe_all", return_value=[denied_probe]):
        second = folder_rescan.reconcile_folders(
            writer, [root], previous_signature=first.signature
        )
        assert second.warning is not None
        assert second.unreadable_roots == [str(root)]
        # The prior live track must survive: a denial is not evidence of absence.
        assert len(_live_tracks(state_conn)) == 1

        third = folder_rescan.reconcile_folders(
            writer, [root], previous_signature=second.signature
        )

    # AC: "fails loudly" -> the warning re-surfaces every cycle, not just once.
    assert third.warning is not None
    assert third.skipped_no_changes is False, "a denied root must be re-evaluated every cycle"
    assert len(_live_tracks(state_conn)) == 1


def test_mass_missing_guard_refuses_when_files_vanish_without_a_denial(
    state_conn, tmp_path: Path
) -> None:
    """Mutation control, opposite direction from the denied-root test: files
    that are simply gone (no TCC denial) from an otherwise-readable root must
    still refuse via LIBM-41, not tombstone everything silently."""
    root = tmp_path / "music"
    for name in ("a.wav", "b.wav", "c.wav"):
        _write_wav(root / name)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    assert first.tracks_added == 3

    for name in ("a.wav", "b.wav", "c.wav"):
        (root / name).unlink()

    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)

    assert second.warning is not None
    assert "LIBM-41" in second.warning
    assert len(_live_tracks(state_conn)) == 3, "guard must block the tombstone, not just warn"


def test_rescan_never_tombstones_a_non_folder_identity_at_a_folder_path(
    state_conn, tmp_path: Path
) -> None:
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav(audio_path)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    inferred_id = _live_tracks(state_conn)[0][0]
    rekordbox_id = "rekordbox-track-at-same-path"
    writer.upsert_track(
        stable_id=rekordbox_id,
        stable_id_tier="isrc",
        title="Imported",
        artists=[],
        album=None,
        isrc="US-TEST-00001",
        duration_ms=None,
        file_path=str(audio_path),
    )

    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)

    assert second.tracks_removed == 0
    assert {row[0] for row in _live_tracks(state_conn)} == {inferred_id, rekordbox_id}


def test_mass_missing_guard_does_not_block_a_genuine_small_removal(
    state_conn, tmp_path: Path
) -> None:
    """Opposite-direction control for the guard above: losing ONE of many
    files is a normal removal and must still go through, proving the guard
    is not simply refusing every cycle after the first."""
    root = tmp_path / "music"
    for name in ("a.wav", "b.wav", "c.wav"):
        _write_wav(root / name)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")

    (root / "a.wav").unlink()
    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)

    assert second.warning is None
    assert second.tracks_removed == 1
    assert len(_live_tracks(state_conn)) == 2


def test_bulk_removal_is_capped_per_cycle_and_drains_over_subsequent_cycles(
    state_conn, tmp_path: Path
) -> None:
    """More removals than ``MAX_TOMBSTONES_PER_CYCLE`` in one pass must not
    burst every tombstone (and its bus event) onto one round: the cap bounds
    one cycle's work, and the signature stays unstable until the backlog
    fully drains across subsequent cycles."""
    root = tmp_path / "music"
    names = [f"track-{i:03d}.wav" for i in range(6)]
    for name in names:
        _write_wav(root / name)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    assert first.tracks_added == 6

    for name in names[:5]:
        (root / name).unlink()

    with mock.patch.object(folder_rescan, "MAX_TOMBSTONES_PER_CYCLE", 2):
        second = folder_rescan.reconcile_folders(
            writer, [root], previous_signature=first.signature, allow_mass_missing=True
        )
        assert second.tracks_removed == 2
        assert second.tombstones_pending == 3
        assert len(_live_tracks(state_conn)) == 4

        third = folder_rescan.reconcile_folders(
            writer, [root], previous_signature=second.signature, allow_mass_missing=True
        )
        assert third.tracks_removed == 2
        assert third.tombstones_pending == 1
        assert len(_live_tracks(state_conn)) == 2

        fourth = folder_rescan.reconcile_folders(
            writer, [root], previous_signature=third.signature, allow_mass_missing=True
        )
        assert fourth.tracks_removed == 1
        assert fourth.tombstones_pending == 0
        assert len(_live_tracks(state_conn)) == 1


def test_allow_mass_missing_overrides_the_guard(state_conn, tmp_path: Path) -> None:
    root = tmp_path / "music"
    for name in ("a.wav", "b.wav", "c.wav"):
        _write_wav(root / name)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")

    for name in ("a.wav", "b.wav", "c.wav"):
        (root / name).unlink()

    second = folder_rescan.reconcile_folders(
        writer, [root], previous_signature=first.signature, allow_mass_missing=True
    )

    assert second.warning is None
    assert second.tracks_removed == 3
    assert _live_tracks(state_conn) == []


# ----- normalization safety ---------------------------------------------------


def test_nfc_nfd_spelling_drift_at_one_path_is_not_flagged_added_or_removed(
    state_conn, tmp_path: Path
) -> None:
    """A tier-3 id hashes the NFC-normalized, case-folded path. If the diff
    ever compared RAW path strings instead of ids, a filename whose Unicode
    form drifts between two walks (no content change) would look both
    removed (old spelling gone) and added (new spelling appeared) on every
    single cycle, forever."""
    root = tmp_path / "music"
    name_nfc = unicodedata.normalize("NFC", "café.wav")
    name_nfd = unicodedata.normalize("NFD", "café.wav")
    assert name_nfc != name_nfd, "test fixture must actually exercise two byte-forms"
    audio_path = root / name_nfc
    _write_wav(audio_path)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    assert first.tracks_added == 1
    live_id = _live_tracks(state_conn)[0][0]

    # Simulate the SAME on-disk file now walked back with the OTHER Unicode
    # form (e.g. a network share or `os.walk` returning it differently),
    # same mtime -- a real rename would be a genuine change, so pin mtime.
    same_mtime = audio_path.stat().st_mtime
    drifted_path = root / name_nfd
    os.rename(audio_path, drifted_path)
    os.utime(drifted_path, (same_mtime, same_mtime))

    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)

    assert second.tracks_added == 0, "no new row: the normalized id is unchanged"
    assert second.tracks_removed == 0, "no tombstone: the normalized id is unchanged"
    live = _live_tracks(state_conn)
    assert len(live) == 1
    assert live[0][0] == live_id


# ----- LIBM-140: a missing file is not a user delete ------------------------


def _tombstone(state_conn, file_path: Path) -> tuple[str | None, str | None, str | None]:
    row = state_conn.execute(
        "SELECT deleted_at, deleted_reason, restored_at FROM tracks WHERE file_path = ?",
        (str(file_path),),
    ).fetchone()
    return (row[0], row[1], row[2])


def test_a_file_that_went_missing_comes_back_when_the_file_does(state_conn, tmp_path: Path) -> None:
    """The rescan's own tombstone records WHY, so finding the file again lifts
    it. Without the reason this is indistinguishable from a user delete and
    the track would stay gone forever."""
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav(audio_path)
    _write_wav(root / "keep.wav")
    original = audio_path.read_bytes()
    stat = audio_path.stat()
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")

    audio_path.unlink()
    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)
    deleted_at, reason, _restored = _tombstone(state_conn, audio_path)
    assert deleted_at is not None
    assert reason == "missing"

    audio_path.write_bytes(original)
    os.utime(audio_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same file, same id
    third = folder_rescan.reconcile_folders(writer, [root], previous_signature=second.signature)

    assert third.tracks_added == 1
    deleted_at, reason, restored_at = _tombstone(state_conn, audio_path)
    assert (deleted_at, reason) == (None, None)
    assert restored_at is not None, "the lift must outrank the tombstone on other machines"
    assert len(_live_tracks(state_conn)) == 2


def test_a_track_the_user_removed_stays_removed_while_its_file_is_still_there(
    state_conn, tmp_path: Path
) -> None:
    """The control for the test above: the same rescan, but the tombstone is
    the user's. The file is on disk every cycle and the track stays removed."""
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav(audio_path)
    _write_wav(root / "keep.wav")
    writer = _writer(state_conn)
    folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    removed_id = next(sid for sid, path in _live_tracks(state_conn) if path == str(audio_path))
    writer.remove_from_library(removed_id)
    before = _tombstone(state_conn, audio_path)
    assert before[1] == "user"

    report = folder_rescan.reconcile_folders(writer, [root], previous_signature="")

    assert report.tracks_added == 0
    assert _tombstone(state_conn, audio_path) == before
    assert [path for _sid, path in _live_tracks(state_conn)] == [str(root / "keep.wav")]


def test_a_tombstone_older_than_the_reason_column_is_read_as_the_users(
    state_conn, tmp_path: Path
) -> None:
    """A tombstone written before schema v23 has no reason. It is kept: an old
    delete stays deleted, and ``undelete`` is the way back if it was a scan's."""
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav(audio_path)
    _write_wav(root / "keep.wav")
    writer = _writer(state_conn)
    folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    removed_id = next(sid for sid, path in _live_tracks(state_conn) if path == str(audio_path))
    writer.remove_from_library(removed_id)
    state_conn.execute("UPDATE tracks SET deleted_reason = NULL WHERE stable_id = ?", (removed_id,))

    report = folder_rescan.reconcile_folders(writer, [root], previous_signature="")

    assert report.tracks_added == 0
    assert _tombstone(state_conn, audio_path)[0] is not None


# ----- overlap with another source (FWATCH-02) ----------------------------


def test_a_file_another_source_already_owns_is_not_duplicated(state_conn, tmp_path: Path) -> None:
    """[if] a watched folder holds a file rekordbox already put in the library
    [then] the cycle mints no folder-tier copy, [else stop].

    A LIVE row under a watched root is part of the rescan inventory, so it is
    adopted rather than counted as owned elsewhere; the counter covers the
    rows the inventory cannot see (a user-removed row, below).
    """
    root = tmp_path / "music"
    owned = root / "owned.wav"
    _write_wav(owned)
    _write_wav(root / "new.wav")
    writer = _writer(state_conn)
    writer.upsert_track(
        stable_id="rb-owned",
        stable_id_tier="isrc",
        title="owned",
        artists=[],
        album=None,
        isrc="GBAAA0000001",
        duration_ms=1,
        file_path=str(owned),
        content_hash=None,
        audio_hash=None,
    )

    report = folder_rescan.reconcile_folders(writer, [root], previous_signature="")

    assert report.files_owned_by_other_source == 0
    assert report.tracks_added == 1
    paths = [path for _, path in _live_tracks(state_conn)]
    assert paths.count(str(owned)) == 1, paths
    assert ("rb-owned", str(owned)) in _live_tracks(state_conn)
    assert str(root / "new.wav") in paths


def test_a_watched_folder_with_no_other_source_adds_every_file(state_conn, tmp_path: Path) -> None:
    """Control for the guard above: with no overlapping row, nothing is held back."""
    root = tmp_path / "music"
    _write_wav(root / "a.wav")
    _write_wav(root / "b.wav")
    report = folder_rescan.reconcile_folders(_writer(state_conn), [root], previous_signature="")
    assert report.files_owned_by_other_source == 0
    assert report.tracks_added == 2


def test_a_file_relinked_away_from_its_other_source_is_picked_up_without_a_disk_change(
    state_conn, tmp_path: Path
) -> None:
    """[if] the other-source row moves to another path while the file stays [then]
    the next cycle does not cheap-skip and the folder row is minted, [else stop]."""
    root = tmp_path / "music"
    owned = root / "owned.wav"
    _write_wav(owned)
    writer = _writer(state_conn)
    _upsert_other_source(writer, "rb-owned", owned)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    assert first.tracks_added == 0
    _upsert_other_source(writer, "rb-owned", tmp_path / "elsewhere.wav")

    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)

    assert second.skipped_no_changes is False
    assert second.tracks_added == 1


def test_a_track_the_user_removed_is_not_brought_back_by_a_watched_folder(
    state_conn, tmp_path: Path
) -> None:
    """[if] the user removed a rekordbox row whose file sits in a watched folder
    [then] no folder-tier copy appears, [else stop]."""
    root = tmp_path / "music"
    owned = root / "owned.wav"
    _write_wav(owned)
    writer = _writer(state_conn)
    _upsert_other_source(writer, "rb-owned", owned)
    writer.remove_from_library("rb-owned")

    report = folder_rescan.reconcile_folders(writer, [root], previous_signature="")

    assert report.tracks_added == 0
    assert report.files_owned_by_other_source == 1
    assert _live_tracks(state_conn) == []


def _upsert_inferred_rekordbox(writer: StateWriter, stable_id: str, path: Path) -> None:
    """A rekordbox track with no ISRC: ``inferred`` tier, owned via its vendor id."""
    writer.upsert_track(
        stable_id=stable_id,
        stable_id_tier="inferred",
        title="owned",
        artists=[],
        album=None,
        isrc=None,
        duration_ms=1,
        file_path=str(path),
        content_hash=None,
        audio_hash=None,
    )
    writer.set_vendor_id(stable_id, "rekordbox", "4711")


def test_an_inferred_rekordbox_row_the_user_removed_is_not_brought_back(
    state_conn, tmp_path: Path
) -> None:
    """[if] the removed rekordbox row has no ISRC (inferred tier) [then] its
    vendor id still marks the path as owned, so no folder row appears, [else stop]."""
    root = tmp_path / "music"
    owned = root / "owned.wav"
    _write_wav(owned)
    writer = _writer(state_conn)
    _upsert_inferred_rekordbox(writer, "rb-inferred", owned)
    writer.remove_from_library("rb-inferred")

    report = folder_rescan.reconcile_folders(writer, [root], previous_signature="")

    assert report.tracks_added == 0
    assert report.files_owned_by_other_source == 1
    assert _live_tracks(state_conn) == []


def test_an_inferred_rekordbox_row_relinked_away_frees_its_file(state_conn, tmp_path: Path) -> None:
    """[if] a removed inferred rekordbox row is restored and relinked away while
    the file stays [then] the next cycle does not cheap-skip and the folder row
    is minted, [else stop]."""
    root = tmp_path / "music"
    owned = root / "owned.wav"
    _write_wav(owned)
    writer = _writer(state_conn)
    _upsert_inferred_rekordbox(writer, "rb-inferred", owned)
    writer.remove_from_library("rb-inferred")
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    assert first.tracks_added == 0
    writer.undelete_track("rb-inferred")
    _upsert_inferred_rekordbox(writer, "rb-inferred", tmp_path / "elsewhere.wav")

    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)

    assert second.skipped_no_changes is False
    assert second.tracks_added == 1


def _upsert_other_source(writer: StateWriter, stable_id: str, path: Path) -> None:
    writer.upsert_track(
        stable_id=stable_id,
        stable_id_tier="isrc",
        title="owned",
        artists=[],
        album=None,
        isrc="GBAAA0000002",
        duration_ms=1,
        file_path=str(path),
        content_hash=None,
        audio_hash=None,
    )


def _write_wav_sample(path: Path, sample: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<h", sample))


@pytest.mark.requirement("NATIVE-30")
def test_a_rewrite_keeps_an_isrc_identity(state_conn, tmp_path: Path) -> None:
    """[if] a rewrite meets an ISRC row [then] tier, ISRC and stored metadata stay, [else stop]."""
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav(audio_path)
    writer = _writer(state_conn)
    writer.upsert_track(
        stable_id="isrc-kept",
        stable_id_tier="isrc",
        title="Kept",
        artists=["A"],
        album=None,
        isrc="USRC17607839",
        duration_ms=1,
        file_path=str(audio_path),
        audio_hash="sha256:" + "11" * 32,
    )
    report = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    row = state_conn.execute(
        "SELECT stable_id, stable_id_tier, isrc, audio_hash FROM tracks "
        "WHERE deleted_at IS NULL"
    ).fetchone()
    assert report.tracks_removed == 0
    assert row[0] == "isrc-kept"
    assert row[1] == "isrc"
    assert row[2] == "USRC17607839"
    assert row[3] != "sha256:" + "11" * 32
    kept = state_conn.execute(
        "SELECT title, artists_json, album, duration_ms FROM tracks WHERE stable_id = 'isrc-kept'"
    ).fetchone()
    assert tuple(kept) == ("Kept", '["A"]', None, 1)


@pytest.mark.requirement("NATIVE-30")
def test_a_rewrite_still_refreshes_an_inferred_rows_title(state_conn, tmp_path: Path) -> None:
    """[if] a rewrite meets an inferred row [then] its title follows the file again, [else stop]."""
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav(audio_path)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    (live_id, _path), = _live_tracks(state_conn)
    state_conn.execute("UPDATE tracks SET title = 'Stale' WHERE stable_id = ?", (live_id,))
    state_conn.commit()

    _write_wav_sample(audio_path, 1234)
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)

    title = state_conn.execute(
        "SELECT title FROM tracks WHERE stable_id = ?", (live_id,)
    ).fetchone()[0]
    assert title == "a"


@pytest.mark.requirement("NATIVE-30")
def test_a_rewrite_that_cannot_be_refreshed_is_counted_in_the_warning(
    state_conn, tmp_path: Path
) -> None:
    """[if] a rewritten file is unplayable [then] the cycle's warning counts it, [else stop]."""
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav(audio_path)
    _write_wav_sample(root / "keep.wav", 4321)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    assert first.warning is None

    audio_path.write_text("This is prose, not audio. " * 100, encoding="utf-8")
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)

    assert second.tracks_removed == 0
    assert second.warning is not None
    assert "1 rewritten file(s) could not be refreshed (1 unplayable)" in second.warning


@pytest.mark.requirement("NATIVE-30")
def test_a_changed_hash_keeps_the_row_and_drops_analysis(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] a rewrite changes the hash [then] the row stays and analysis is dropped, [else stop]."""
    from datetime import UTC, datetime

    from apps.analysis.lanes import LaneResult
    from apps.analysis.record import AnalysisRecord
    from apps.analysis.store import upsert_record

    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav_sample(audio_path, 0)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    stable_id = _live_tracks(state_conn)[0][0]
    old_hash = state_conn.execute(
        "SELECT audio_hash FROM tracks WHERE stable_id = ?", (stable_id,)
    ).fetchone()[0]
    upsert_record(
        AnalysisRecord(
            stable_id=stable_id,
            backend="own_beatgrid.backfill",
            backend_version="1.6.0",
            analyzed_at=datetime(2026, 10, 8, tzinfo=UTC),
            duration_s=1.0,
            sample_rate=44100,
            bpm=120.0,
            bpm_confidence=0.9,
            key_camelot="8A",
            key_openkey="1m",
            key_confidence=0.8,
            energy=5,
            producer="backfill",
            producer_version="1.6.0",
            uses_model=False,
            decode_fingerprint="sha256:" + "66" * 32,
            lanes={
                "beatgrid": LaneResult(
                    status="failed", reason="no_trackable_pulse", payload={}
                )
            },
        ),
        db_path=state_db_path,
    )
    assert state_conn.execute(
        "SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (stable_id,)
    ).fetchone()[0] == 1

    _write_wav_sample(audio_path, 2000)
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    second = folder_rescan.reconcile_folders(
        writer, [root], previous_signature=first.signature
    )
    live = _live_tracks(state_conn)
    new_hash = state_conn.execute(
        "SELECT audio_hash FROM tracks WHERE stable_id = ?", (stable_id,)
    ).fetchone()[0]
    assert second.tracks_removed == 0
    assert len(live) == 1 and live[0][0] == stable_id
    assert new_hash != old_hash
    assert state_conn.execute(
        "SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (stable_id,)
    ).fetchone()[0] == 0


@pytest.mark.requirement("NATIVE-30")
@pytest.mark.requirement("NATIVE-30")
def test_a_rewrite_drops_analysis_on_every_live_row_at_the_path(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] a rewrite meets two live rows [then] both hashes change and both analyses drop, [else stop]."""
    from datetime import UTC, datetime

    from apps.analysis.lanes import LaneResult
    from apps.analysis.record import AnalysisRecord
    from apps.analysis.store import upsert_record

    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav_sample(audio_path, 0)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    inferred_id = _live_tracks(state_conn)[0][0]
    old_inferred_hash = state_conn.execute(
        "SELECT audio_hash FROM tracks WHERE stable_id = ?", (inferred_id,)
    ).fetchone()[0]
    writer.upsert_track(
        stable_id="isrc-twin",
        stable_id_tier="isrc",
        title="Twin",
        artists=["A"],
        album=None,
        isrc="USRC17607839",
        duration_ms=1,
        file_path=str(audio_path),
        audio_hash="sha256:" + "11" * 32,
    )

    def _failed(stable_id: str) -> AnalysisRecord:
        return AnalysisRecord(
            stable_id=stable_id,
            backend="own_beatgrid.backfill",
            backend_version="1.6.0",
            analyzed_at=datetime(2026, 10, 8, tzinfo=UTC),
            duration_s=1.0,
            sample_rate=44100,
            bpm=120.0,
            bpm_confidence=0.9,
            key_camelot="8A",
            key_openkey="1m",
            key_confidence=0.8,
            energy=5,
            producer="backfill",
            producer_version="1.6.0",
            uses_model=False,
            decode_fingerprint="sha256:" + "66" * 32,
            lanes={
                "beatgrid": LaneResult(
                    status="failed", reason="no_trackable_pulse", payload={}
                )
            },
        )

    upsert_record(_failed(inferred_id), db_path=state_db_path)
    upsert_record(_failed("isrc-twin"), db_path=state_db_path)
    _write_wav_sample(audio_path, 2000)
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)

    live = _live_tracks(state_conn)
    assert sorted(sid for sid, _path in live) == sorted([inferred_id, "isrc-twin"])
    hashes = {
        sid: state_conn.execute(
            "SELECT audio_hash FROM tracks WHERE stable_id = ?", (sid,)
        ).fetchone()[0]
        for sid, _path in live
    }
    assert hashes[inferred_id] != old_inferred_hash
    assert hashes["isrc-twin"] != "sha256:" + "11" * 32
    assert hashes[inferred_id] == hashes["isrc-twin"]
    for sid, _path in live:
        assert state_conn.execute(
            "SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (sid,)
        ).fetchone()[0] == 0
    kept = state_conn.execute(
        "SELECT stable_id_tier, isrc FROM tracks WHERE stable_id = 'isrc-twin'"
    ).fetchone()
    assert kept == ("isrc", "USRC17607839")


def test_a_spelling_drift_plus_mtime_keeps_the_same_row(state_conn, tmp_path: Path) -> None:
    """[if] path spelling drifts and mtime moves [then] the same row stays, [else stop]."""
    root = tmp_path / "music"
    name_nfc = unicodedata.normalize("NFC", "caf" + chr(0xE9) + ".wav")
    name_nfd = unicodedata.normalize("NFD", name_nfc)
    assert name_nfc != name_nfd
    audio_path = root / name_nfc
    _write_wav(audio_path)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    live_id = _live_tracks(state_conn)[0][0]
    drifted = root / name_nfd
    os.rename(audio_path, drifted)
    bumped = drifted.stat().st_mtime + 5
    os.utime(drifted, (bumped, bumped))
    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)
    live = _live_tracks(state_conn)
    assert second.tracks_removed == 0
    assert len(live) == 1
    assert live[0][0] == live_id


@pytest.mark.requirement("NATIVE-30")
def test_a_missing_file_tombstones_an_adopted_isrc_row(state_conn, tmp_path: Path) -> None:
    """[if] an adopted ISRC row's file is gone [then] that row is tombstoned, [else stop]."""
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav(audio_path)
    _write_wav(root / "keep.wav")
    writer = _writer(state_conn)
    writer.upsert_track(
        stable_id="isrc-gone",
        stable_id_tier="isrc",
        title="Gone",
        artists=["A"],
        album=None,
        isrc="USRC17607839",
        duration_ms=1,
        file_path=str(audio_path),
        audio_hash="sha256:" + "11" * 32,
    )
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    assert any(sid == "isrc-gone" for sid, _path in _live_tracks(state_conn))
    audio_path.unlink()
    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)
    assert second.tracks_removed == 1
    assert [path for _sid, path in _live_tracks(state_conn)] == [str(root / "keep.wav")]


def _seed_isrc_row_and_reconcile(state_conn, root: Path) -> tuple[StateWriter, Path, str]:
    """An ISRC row at ``a.wav`` beside ``keep.wav``, after one reconcile.

    The two files hold different audio. A removed-track match is by audio
    hash and stands down while a live row carries the same hash.
    """
    audio_path = root / "a.wav"
    _write_wav_sample(audio_path, 1234)
    _write_wav(root / "keep.wav")
    writer = _writer(state_conn)
    writer.upsert_track(
        stable_id="isrc-gone",
        stable_id_tier="isrc",
        title="Gone",
        artists=["A"],
        album=None,
        isrc="USRC17607839",
        duration_ms=1,
        file_path=str(audio_path),
        audio_hash="sha256:" + "11" * 32,
    )
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    return writer, audio_path, first.signature


@pytest.mark.requirement("NATIVE-30")
def test_a_returning_file_restores_its_tombstoned_isrc_row(state_conn, tmp_path: Path) -> None:
    """[if] a tombstoned ISRC row's file returns [then] that row is live again, alone, [else stop]."""
    root = tmp_path / "music"
    writer, audio_path, signature = _seed_isrc_row_and_reconcile(state_conn, root)
    audio_path.unlink()
    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=signature)
    assert second.tracks_removed == 1
    assert all(sid != "isrc-gone" for sid, _path in _live_tracks(state_conn))

    _write_wav_sample(audio_path, 1234)
    third = folder_rescan.reconcile_folders(writer, [root], previous_signature=second.signature)

    assert third.tracks_added == 1
    at_path = [sid for sid, path in _live_tracks(state_conn) if path == str(audio_path)]
    assert at_path == ["isrc-gone"]
    row = state_conn.execute(
        "SELECT stable_id_tier, isrc, deleted_at, deleted_reason FROM tracks "
        "WHERE stable_id = 'isrc-gone'"
    ).fetchone()
    assert tuple(row) == ("isrc", "USRC17607839", None, None)


@pytest.mark.requirement("NATIVE-30")
def test_a_rescan_does_not_restore_an_isrc_row_the_user_removed(
    state_conn, tmp_path: Path
) -> None:
    """[if] the user removed an ISRC row [then] a rescan of its file leaves it removed, [else stop]."""
    root = tmp_path / "music"
    writer, _audio_path, _signature = _seed_isrc_row_and_reconcile(state_conn, root)
    writer.remove_from_library("isrc-gone")

    folder_rescan.reconcile_folders(writer, [root], previous_signature="")

    assert [path for _sid, path in _live_tracks(state_conn)] == [str(root / "keep.wav")]
    reason = state_conn.execute(
        "SELECT deleted_reason FROM tracks WHERE stable_id = 'isrc-gone'"
    ).fetchone()[0]
    assert reason == "user"


@pytest.mark.requirement("NATIVE-30")
def test_a_missing_prior_hash_drops_analysis_on_rewrite(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] a rewrite fills a missing hash [then] the old analysis is dropped, [else stop]."""
    from datetime import UTC, datetime

    from apps.analysis.lanes import LaneResult
    from apps.analysis.record import AnalysisRecord
    from apps.analysis.store import upsert_record

    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav_sample(audio_path, 0)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    stable_id = _live_tracks(state_conn)[0][0]
    state_conn.execute("UPDATE tracks SET audio_hash = NULL WHERE stable_id = ?", (stable_id,))
    state_conn.commit()
    upsert_record(
        AnalysisRecord(
            stable_id=stable_id,
            backend="own_beatgrid.backfill",
            backend_version="1.6.0",
            analyzed_at=datetime(2026, 10, 8, tzinfo=UTC),
            duration_s=1.0,
            sample_rate=44100,
            bpm=120.0,
            bpm_confidence=0.9,
            key_camelot="8A",
            key_openkey="1m",
            key_confidence=0.8,
            energy=5,
            producer="backfill",
            producer_version="1.6.0",
            uses_model=False,
            decode_fingerprint="sha256:" + "66" * 32,
            lanes={
                "beatgrid": LaneResult(
                    status="failed", reason="no_trackable_pulse", payload={}
                )
            },
        ),
        db_path=state_db_path,
    )
    _write_wav_sample(audio_path, 2000)
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)
    assert state_conn.execute(
        "SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (stable_id,)
    ).fetchone()[0] == 0


@pytest.mark.requirement("NATIVE-30")
def test_the_same_hash_on_refresh_keeps_analysis(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] a refresh writes the same hash [then] the analysis stays, [else stop]."""
    from datetime import UTC, datetime

    from apps.analysis.lanes import LaneResult
    from apps.analysis.record import AnalysisRecord
    from apps.analysis.store import upsert_record

    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav_sample(audio_path, 0)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    stable_id = _live_tracks(state_conn)[0][0]
    upsert_record(
        AnalysisRecord(
            stable_id=stable_id,
            backend="own_beatgrid.backfill",
            backend_version="1.6.0",
            analyzed_at=datetime(2026, 10, 8, tzinfo=UTC),
            duration_s=1.0,
            sample_rate=44100,
            bpm=120.0,
            bpm_confidence=0.9,
            key_camelot="8A",
            key_openkey="1m",
            key_confidence=0.8,
            energy=5,
            producer="backfill",
            producer_version="1.6.0",
            uses_model=False,
            decode_fingerprint="sha256:" + "66" * 32,
            lanes={
                "beatgrid": LaneResult(
                    status="failed", reason="no_trackable_pulse", payload={}
                )
            },
        ),
        db_path=state_db_path,
    )
    bumped = audio_path.stat().st_mtime + 5
    os.utime(audio_path, (bumped, bumped))
    folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)
    assert state_conn.execute(
        "SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (stable_id,)
    ).fetchone()[0] == 1


def _seed_analysis(state_db_path: Path, stable_id: str, analyzed_at) -> None:
    from apps.analysis.lanes import LaneResult
    from apps.analysis.record import AnalysisRecord
    from apps.analysis.store import upsert_record

    upsert_record(
        AnalysisRecord(
            stable_id=stable_id,
            backend="own_beatgrid.backfill",
            backend_version="1.6.0",
            analyzed_at=analyzed_at,
            duration_s=1.0,
            sample_rate=44100,
            bpm=120.0,
            bpm_confidence=0.9,
            key_camelot="8A",
            key_openkey="1m",
            key_confidence=0.8,
            energy=5,
            producer="backfill",
            producer_version="1.6.0",
            uses_model=False,
            decode_fingerprint="sha256:" + "66" * 32,
            lanes={"beatgrid": LaneResult(status="failed", reason="no_trackable_pulse", payload={})},
        ),
        db_path=state_db_path,
    )


def _refresh_untouched_bytes(
    state_conn,
    state_db_path: Path,
    tmp_path: Path,
    *,
    analyzed_after_write: bool,
    stored_hash: str | None,
    stored_duration_shift_ms: int = 0,
    naive_stamp: bool = False,
    replaced_keeping_mtime: bool = False,
) -> int:
    """Refresh a row whose bytes did not change, and count its analysis records after.

    The file's mtime is moved back an hour, so the rescan mints another id and
    refreshes the row, while the audio bytes stay the same. "After the write"
    means after the file's last change, inode change included, so that stamp
    follows the ``utime`` call. ``replaced_keeping_mtime`` then swaps in
    another recording of the same length and restores the old mtime, as
    ``cp -p`` does.
    """
    import time
    from datetime import UTC, datetime, timedelta

    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav_sample(audio_path, 0)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    stable_id = _live_tracks(state_conn)[0][0]
    state_conn.execute(
        "UPDATE tracks SET audio_hash = ?, duration_ms = duration_ms + ? WHERE stable_id = ?",
        (stored_hash, stored_duration_shift_ms, stable_id),
    )
    state_conn.commit()
    moved = audio_path.stat().st_mtime - 3600
    os.utime(audio_path, (moved, moved))
    written = datetime.fromtimestamp(moved, tz=UTC)
    changed = datetime.fromtimestamp(audio_path.stat().st_ctime, tz=UTC)
    if replaced_keeping_mtime:
        analyzed = written + timedelta(minutes=1)
    elif analyzed_after_write:
        analyzed = max(written, changed) + timedelta(milliseconds=500)
    else:
        analyzed = written - timedelta(minutes=1)
    _seed_analysis(state_db_path, stable_id, analyzed)
    if replaced_keeping_mtime:
        _write_wav_sample(audio_path, 2000)
        os.utime(audio_path, (moved, moved))
    time.sleep(1.0)  # the scan reads the file after the stamp above
    if naive_stamp:
        import sqlite3

        with sqlite3.connect(state_db_path) as raw:
            changed = raw.execute(
                "UPDATE analysis SET analyzed_at = replace(replace(analyzed_at, 'Z', ''), '+00:00', '') "
                "WHERE stable_id = ?",
                (stable_id,),
            ).rowcount
        assert changed == 1, "the control: one stored stamp lost its zone"
    folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)
    assert state_conn.execute(
        "SELECT audio_hash FROM tracks WHERE stable_id = ?", (stable_id,)
    ).fetchone()[0], "the control: the refresh ran and wrote a hash"
    return state_conn.execute(
        "SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (stable_id,)
    ).fetchone()[0]


@pytest.mark.requirement("NATIVE-30")
def test_a_missing_prior_hash_keeps_analysis_made_after_the_file_was_written(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] a row with no stored hash is refreshed and its analysis postdates the file [then] it is kept, [else stop].

    Dropping on a missing hash alone erases every such row's analysis on the
    first rescan that walks its folder (Sol P1 on #5886).
    """
    kept = _refresh_untouched_bytes(
        state_conn, state_db_path, tmp_path, analyzed_after_write=True, stored_hash=None
    )
    assert kept == 1, "if a missing hash alone drops analysis newer than the file then broken"


@pytest.mark.requirement("NATIVE-30")
def test_control_a_missing_prior_hash_drops_analysis_older_than_the_file(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] the analysis predates the file's last write and no hash was stored [then] it is dropped, [else stop]."""
    kept = _refresh_untouched_bytes(
        state_conn, state_db_path, tmp_path, analyzed_after_write=False, stored_hash=None
    )
    assert kept == 0, "if analysis older than the file survives a hash-less refresh then broken"


@pytest.mark.requirement("NATIVE-30")
def test_a_missing_prior_hash_drops_analysis_of_bytes_replaced_with_the_old_mtime(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] another recording of the same length replaces the file and keeps its mtime [then] the analysis drops, [else stop].

    The mtime alone says the analysis is newer than the file; the inode
    change the copy made says otherwise (Sol P1 on #5886).
    """
    kept = _refresh_untouched_bytes(
        state_conn, state_db_path, tmp_path, analyzed_after_write=True, stored_hash=None,
        replaced_keeping_mtime=True,
    )
    assert kept == 0, "if a replacement that restored the old mtime keeps the old analysis then broken"


@pytest.mark.requirement("NATIVE-30")
def test_a_missing_prior_hash_with_a_zoneless_stamp_drops_analysis_however_new(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] no hash is stored and the analysis stamp has no zone [then] it drops even if newer, [else stop].

    A naive analyzed_at is unreadable, not UTC: read as UTC, a local-time
    stamp could pass the mtime rule and keep analysis of replaced bytes
    (Sol P1 on #5886). The aware twin above keeps the same analysis.
    """
    kept = _refresh_untouched_bytes(
        state_conn, state_db_path, tmp_path, analyzed_after_write=True, stored_hash=None, naive_stamp=True
    )
    assert kept == 0, "if a zoneless stamp is read as UTC and keeps analysis then broken"


@pytest.mark.requirement("NATIVE-30")
def test_a_missing_prior_hash_and_another_duration_drops_analysis_however_new(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] no hash is stored and the length differs [then] newer analysis drops, [else stop].

    A replacement that keeps or backdates the old mtime passes the mtime
    rule, so a second recording's length must not (Sol P1 on #5886).
    """
    kept = _refresh_untouched_bytes(
        state_conn, state_db_path, tmp_path, analyzed_after_write=True, stored_hash=None,
        stored_duration_shift_ms=5000,
    )
    assert kept == 0, "if a hash-less row of another length keeps its analysis then broken"


@pytest.mark.requirement("NATIVE-30")
def test_control_a_different_stored_hash_drops_analysis_however_new(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] a stored hash differs from the file's [then] the analysis is dropped even if newer, [else stop].

    The overshoot of the mtime rule is to let it overrule a hash that proves
    the bytes changed.
    """
    kept = _refresh_untouched_bytes(
        state_conn, state_db_path, tmp_path, analyzed_after_write=True, stored_hash="sha256:" + "99" * 32
    )
    assert kept == 0, "if a newer timestamp overrules a different hash then broken"


def _serving(state_conn, stable_id: str) -> tuple[int, int]:
    """How many lanes of ``stable_id`` the canonical pointer serves, and how many are stale."""
    served = state_conn.execute(
        "SELECT COUNT(*) FROM analysis_canonical WHERE stable_id = ?", (stable_id,)
    ).fetchone()[0]
    stale = state_conn.execute(
        "SELECT COUNT(*) FROM analysis_stale WHERE stable_id = ?", (stable_id,)
    ).fetchone()[0]
    return served, stale


def _rewrite_three_analyzed_files(
    state_conn, state_db_path: Path, tmp_path: Path, *, max_analysis_drops: int
) -> tuple[folder_rescan.FolderRescanReport, dict[str, int], dict[str, bool]]:
    """Rewrite three analyzed files with new bytes under ``max_analysis_drops``.

    Returns the second cycle's report, analysis records per row, and whether
    each row's stored hash moved.
    """
    from datetime import UTC, datetime

    root = tmp_path / "music"
    paths = [root / f"t{i}.wav" for i in range(3)]
    for path in paths:
        _write_wav_sample(path, 0)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    before = dict(state_conn.execute("SELECT stable_id, audio_hash FROM tracks").fetchall())
    for stable_id in before:
        _seed_analysis(state_db_path, stable_id, datetime(2026, 10, 8, tzinfo=UTC))
        assert _serving(state_conn, stable_id)[0] > 0, "the control: every seeded row is served"
    for index, path in enumerate(paths, start=1):
        _write_wav_sample(path, 1000 * index)
        os.utime(path, (os.path.getmtime(path) + 5, os.path.getmtime(path) + 5))
    second = folder_rescan.reconcile_folders(
        writer, [root], previous_signature=first.signature, max_analysis_drops=max_analysis_drops
    )
    after = dict(state_conn.execute("SELECT stable_id, audio_hash FROM tracks").fetchall())
    records = {
        sid: state_conn.execute("SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (sid,)).fetchone()[0]
        for sid in before
    }
    return second, records, {sid: after[sid] != before[sid] for sid in before}


@pytest.mark.requirement("NATIVE-30")
def test_a_rescan_over_the_analysis_drop_bound_keeps_the_excess_and_reports_it(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] drops exceed the bound [then] excess analysis is kept and counted, [else stop]."""
    report, records, moved = _rewrite_three_analyzed_files(
        state_conn, state_db_path, tmp_path, max_analysis_drops=1
    )
    assert sorted(records.values()) == [0, 1, 1], "if the excess rows lost analysis then broken"
    assert report.analysis_drops_withheld == 2
    assert report.warning is not None and "2 rewritten row(s) over the analysis drop bound of 1" in report.warning
    assert report.signature == "", "the withheld rows must be refreshed again next cycle"
    for sid, count in records.items():
        assert moved[sid] == (count == 0), "a withheld row keeps its stored hash"
    for sid in (sid for sid, count in records.items() if count):
        served, stale = _serving(state_conn, sid)
        assert served == 0, "if a withheld row still serves analysis of the old bytes then broken"
        assert stale > 0, "a withheld row's kept records are marked stale"


@pytest.mark.requirement("NATIVE-30")
def test_a_withheld_path_counts_every_row_that_shares_it(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] a withheld path has two analyzed rows [then] both are counted and kept, [else stop]."""
    from datetime import UTC, datetime

    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav_sample(audio_path, 0)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    writer.upsert_track(
        stable_id="isrc-twin",
        stable_id_tier="isrc",
        title="Twin",
        artists=["A"],
        album=None,
        isrc="USRC17607839",
        duration_ms=1,
        file_path=str(audio_path),
        audio_hash="sha256:" + "11" * 32,
    )
    before = dict(state_conn.execute("SELECT stable_id, audio_hash FROM tracks").fetchall())
    for stable_id in before:
        _seed_analysis(state_db_path, stable_id, datetime(2026, 10, 8, tzinfo=UTC))
    _write_wav_sample(audio_path, 2000)
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    report = folder_rescan.reconcile_folders(
        writer, [root], previous_signature=first.signature, max_analysis_drops=0
    )
    assert report.analysis_drops_withheld == 2, "if only the primary row is counted then broken"
    assert dict(state_conn.execute("SELECT stable_id, audio_hash FROM tracks").fetchall()) == before
    for stable_id in before:
        assert state_conn.execute(
            "SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (stable_id,)
        ).fetchone()[0] == 1


@pytest.mark.requirement("NATIVE-30")
def test_control_a_rescan_at_the_analysis_drop_bound_drops_every_analysis(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] drops are at the bound [then] every rewritten row loses analysis, [else stop]."""
    report, records, moved = _rewrite_three_analyzed_files(
        state_conn, state_db_path, tmp_path, max_analysis_drops=3
    )
    assert list(records.values()) == [0, 0, 0]
    assert all(moved.values())
    assert report.analysis_drops_withheld == 0
    assert report.warning is None


@pytest.mark.requirement("NATIVE-30")
def test_a_rewritten_row_with_no_analysis_does_not_spend_the_drop_budget(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] a rewritten row has no analysis [then] it refreshes without spending the drop bound, [else stop]."""
    from datetime import UTC, datetime

    root = tmp_path / "music"
    paths = [root / f"t{i}.wav" for i in range(3)]
    for path in paths:
        _write_wav_sample(path, 0)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    before = dict(state_conn.execute("SELECT stable_id, audio_hash FROM tracks").fetchall())
    analyzed = sorted(before)[0]
    _seed_analysis(state_db_path, analyzed, datetime(2026, 10, 8, tzinfo=UTC))
    for index, path in enumerate(paths, start=1):
        _write_wav_sample(path, 1000 * index)
        os.utime(path, (os.path.getmtime(path) + 5, os.path.getmtime(path) + 5))
    report = folder_rescan.reconcile_folders(
        writer, [root], previous_signature=first.signature, max_analysis_drops=0
    )
    after = dict(state_conn.execute("SELECT stable_id, audio_hash FROM tracks").fetchall())
    assert report.analysis_drops_withheld == 1, "only the analyzed row may be counted against the bound"
    for sid, hash_before in before.items():
        if sid == analyzed:
            assert after[sid] == hash_before, "control: the analyzed row over the bound is still withheld"
        else:
            assert after[sid] != hash_before, "if a row with no analysis is withheld for the bound then broken"


@pytest.mark.requirement("NATIVE-30")
def test_a_rewrite_keeps_a_fingerprint_content_hash(state_conn, tmp_path: Path) -> None:
    """[if] a rewrite adopts a fingerprint row [then] its content hash stays, [else stop]."""
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav(audio_path)
    writer = _writer(state_conn)
    digest = "sha256:" + "ab" * 32
    writer.upsert_track(
        stable_id="fp-kept",
        stable_id_tier="fingerprint",
        title="Kept",
        artists=["A"],
        album=None,
        isrc=None,
        duration_ms=1,
        file_path=str(audio_path),
        content_hash=digest,
        audio_hash="sha256:" + "11" * 32,
    )
    report = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    row = state_conn.execute(
        "SELECT stable_id, stable_id_tier, content_hash FROM tracks WHERE deleted_at IS NULL"
    ).fetchone()
    assert report.tracks_removed == 0
    assert row[0] == "fp-kept"
    assert row[1] == "fingerprint"
    assert row[2] == digest


@pytest.mark.requirement("NATIVE-30")
@pytest.mark.parametrize("stored_artists", ['{"name":"not-a-list"}', None, ""])
def test_a_non_list_artists_json_fails_the_sibling_refresh(
    state_conn, tmp_path: Path, stored_artists: str | None
) -> None:
    """[if] artists_json is not a list (NULL and "" included) [then] refresh raises and keeps it, [else stop]."""
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav_sample(audio_path, 0)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    inferred_id = _live_tracks(state_conn)[0][0]
    writer.upsert_track(
        stable_id="isrc-strong",
        stable_id_tier="isrc",
        title="Strong",
        artists=["A"],
        album=None,
        isrc="USRC17607839",
        duration_ms=1,
        file_path=str(audio_path),
        audio_hash="sha256:" + "11" * 32,
    )
    state_conn.execute(
        "UPDATE tracks SET artists_json = ? WHERE stable_id = ?",
        (stored_artists, inferred_id),
    )
    state_conn.commit()
    _write_wav_sample(audio_path, 2000)
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    with pytest.raises(ValueError, match="artists_json"):
        folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)
    stored = state_conn.execute(
        "SELECT artists_json FROM tracks WHERE stable_id = ?",
        (inferred_id,),
    ).fetchone()[0]
    assert stored == stored_artists, "if a malformed value is replaced (by [] or anything) then broken"


@pytest.mark.requirement("NATIVE-30")
def test_a_folder_import_bounds_analysis_drops_like_a_rescan(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] an import rewrites past the drop bound [then] the excess is kept unserved, [else stop]."""
    from datetime import UTC, datetime

    from apps.shared.state.ingest import folder

    root = tmp_path / "music"
    paths = [root / f"t{i:03}.wav" for i in range(folder.MAX_ANALYSIS_DROPS_PER_CYCLE + 1)]
    for path in paths:
        _write_wav_sample(path, 0)
    writer = _writer(state_conn)
    folder.ingest_folder(writer, [root], dry_run=False)
    ids = [sid for sid, _path in _live_tracks(state_conn)]
    for stable_id in ids:
        _seed_analysis(state_db_path, stable_id, datetime(2026, 10, 8, tzinfo=UTC))
    assert all(_serving(state_conn, sid)[0] for sid in ids), "the control: every seeded row is served"
    for path in paths:
        _write_wav_sample(path, 2000)
        os.utime(path, (os.path.getmtime(path) + 5, os.path.getmtime(path) + 5))
    report = folder.ingest_folder(writer, [root], dry_run=False)
    kept = [sid for sid in ids if state_conn.execute(
        "SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (sid,)
    ).fetchone()[0]]
    assert report.analysis_drops_withheld == 1, "if the import drops past the bound then broken"
    assert len(kept) == 1, "only the one row over the bound keeps its records"
    assert not any(_serving(state_conn, sid)[0] for sid in ids), "no row serves the old bytes' analysis"
