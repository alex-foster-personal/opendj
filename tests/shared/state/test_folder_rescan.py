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
def test_a_non_list_artists_json_fails_the_sibling_refresh(
    state_conn, tmp_path: Path
) -> None:
    """[if] artists_json is not a list [then] refresh raises and keeps it, [else stop]."""
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
        ('{"name":"not-a-list"}', inferred_id),
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
    assert stored == '{"name":"not-a-list"}'
