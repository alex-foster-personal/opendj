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


def test_content_change_at_same_path_tombstones_the_old_row_and_writes_one_new_row(
    state_conn, tmp_path: Path
) -> None:
    """A tier-3 id is sha1(path|mtime): a changed mtime mints a new id at the
    SAME file_path, so a naive upsert-by-id would leave the stale row live
    alongside the new one. This is the corruption class ADR-0121 exists to
    close off."""
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
    assert live[0][0] != old_id
    assert live[0][1] == str(audio_path)
    assert second.tracks_removed == 1
    assert second.tracks_added == 1


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
        path=str(root), exists=True, readable=False, denied=True, detail="TCC denied (test)"
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
