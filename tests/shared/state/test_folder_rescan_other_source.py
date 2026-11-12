"""FWATCH-02 overlap, rewrite, and NATIVE-30 tests for folder rescan.

Continuation of ``test_folder_rescan.py``, split because that file crossed the
1500-line ``file_size`` ratchet (issue #2540). Contract remains
``apps/shared/state/ingest/folder_rescan.py``. Nothing was cut.
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

import pytest

from apps.shared.state.ingest import folder_rescan
from tests.shared.state.folder_rescan_other_source_helpers import (
    _seed_isrc_row_and_reconcile,
    _upsert_inferred_rekordbox,
    _upsert_other_source,
)

from .test_folder_rescan import _live_tracks, _write_wav, _write_wav_sample, _writer


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


def test_a_track_the_user_removed_is_not_brought_back_by_a_watched_folder(state_conn, tmp_path: Path) -> None:
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


def test_an_inferred_rekordbox_row_the_user_removed_is_not_brought_back(state_conn, tmp_path: Path) -> None:
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
        "SELECT stable_id, stable_id_tier, isrc, audio_hash FROM tracks WHERE deleted_at IS NULL"
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
    ((live_id, _path),) = _live_tracks(state_conn)
    state_conn.execute("UPDATE tracks SET title = 'Stale' WHERE stable_id = ?", (live_id,))
    state_conn.commit()

    _write_wav_sample(audio_path, 1234)
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)

    title = state_conn.execute("SELECT title FROM tracks WHERE stable_id = ?", (live_id,)).fetchone()[0]
    assert title == "a"


@pytest.mark.requirement("NATIVE-30")
def test_a_rewrite_that_cannot_be_refreshed_is_counted_in_the_warning(state_conn, tmp_path: Path) -> None:
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
def test_a_changed_hash_keeps_the_row_and_drops_analysis(state_conn, state_db_path: Path, tmp_path: Path) -> None:
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
    old_hash = state_conn.execute("SELECT audio_hash FROM tracks WHERE stable_id = ?", (stable_id,)).fetchone()[0]
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
            lanes={"beatgrid": LaneResult(status="failed", reason="no_trackable_pulse", payload={})},
        ),
        db_path=state_db_path,
    )
    assert state_conn.execute("SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (stable_id,)).fetchone()[0] == 1

    _write_wav_sample(audio_path, 2000)
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    second = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)
    live = _live_tracks(state_conn)
    new_hash = state_conn.execute("SELECT audio_hash FROM tracks WHERE stable_id = ?", (stable_id,)).fetchone()[0]
    assert second.tracks_removed == 0
    assert len(live) == 1 and live[0][0] == stable_id
    assert new_hash != old_hash
    assert state_conn.execute("SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (stable_id,)).fetchone()[0] == 0


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
            lanes={"beatgrid": LaneResult(status="failed", reason="no_trackable_pulse", payload={})},
        )

    upsert_record(_failed(inferred_id), db_path=state_db_path)
    upsert_record(_failed("isrc-twin"), db_path=state_db_path)
    _write_wav_sample(audio_path, 2000)
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)

    live = _live_tracks(state_conn)
    assert sorted(sid for sid, _path in live) == sorted([inferred_id, "isrc-twin"])
    hashes = {
        sid: state_conn.execute("SELECT audio_hash FROM tracks WHERE stable_id = ?", (sid,)).fetchone()[0]
        for sid, _path in live
    }
    assert hashes[inferred_id] != old_inferred_hash
    assert hashes["isrc-twin"] != "sha256:" + "11" * 32
    assert hashes[inferred_id] == hashes["isrc-twin"]
    for sid, _path in live:
        assert state_conn.execute("SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (sid,)).fetchone()[0] == 0
    kept = state_conn.execute("SELECT stable_id_tier, isrc FROM tracks WHERE stable_id = 'isrc-twin'").fetchone()
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
        "SELECT stable_id_tier, isrc, deleted_at, deleted_reason FROM tracks WHERE stable_id = 'isrc-gone'"
    ).fetchone()
    assert tuple(row) == ("isrc", "USRC17607839", None, None)


@pytest.mark.requirement("NATIVE-30")
def test_a_rescan_does_not_restore_an_isrc_row_the_user_removed(state_conn, tmp_path: Path) -> None:
    """[if] the user removed an ISRC row [then] a rescan of its file leaves it removed, [else stop]."""
    root = tmp_path / "music"
    writer, _audio_path, _signature = _seed_isrc_row_and_reconcile(state_conn, root)
    writer.remove_from_library("isrc-gone")

    folder_rescan.reconcile_folders(writer, [root], previous_signature="")

    assert [path for _sid, path in _live_tracks(state_conn)] == [str(root / "keep.wav")]
    reason = state_conn.execute("SELECT deleted_reason FROM tracks WHERE stable_id = 'isrc-gone'").fetchone()[0]
    assert reason == "user"


@pytest.mark.requirement("NATIVE-30")
def test_a_missing_prior_hash_drops_analysis_on_rewrite(state_conn, state_db_path: Path, tmp_path: Path) -> None:
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
            lanes={"beatgrid": LaneResult(status="failed", reason="no_trackable_pulse", payload={})},
        ),
        db_path=state_db_path,
    )
    _write_wav_sample(audio_path, 2000)
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)
    assert state_conn.execute("SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (stable_id,)).fetchone()[0] == 0
