"""Regression tests for the analysis-backed performance rescue fixture (#1735)."""
from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import pytest

from apps.analysis.record import AnalysisRecord
from apps.analysis.store import fetch_records
from apps.shared import audio_files
from apps.shared.state import db as state_db
from apps.webui.frontend.tests.e2e.support.deckload_fixture import (
    ARTWORK_PNG_SHA256,
    ARTWORK_SOURCE_PNG,
    FIXTURE_TRACKS,
    PERFORMANCE_FOLD_TRACK,
    RESCUE_PLAYBACK_ARTWORK_TRACK,
    RESCUE_PLAYBACK_LIBRARY_TRACKS,
    RESCUE_PLAYBACK_TRACKS,
    build_rescue_playback,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _id3_apic_frames(wav_path: Path) -> list[object]:
    from mutagen.wave import WAVE

    audio = WAVE(wav_path)
    if audio.tags is None:
        return []
    return list(audio.tags.getall("APIC") or [])


@pytest.mark.requirement("PARITY-10")
def test_rescue_playback_fixture_writes_measured_librosa_downbeats(tmp_path: Path) -> None:
    """[if] rescue audio is analyzed [then] downbeats persist in state.db, [else stop]."""
    data_dir = tmp_path / "fixture-data"
    rows = build_rescue_playback(data_dir)
    assert len(rows) == len(RESCUE_PLAYBACK_LIBRARY_TRACKS)

    state_db = data_dir / "state" / "state.db"
    stable_ids = [stable_id for stable_id, _title, _file_path in rows]
    stored = fetch_records(stable_ids=stable_ids, backend="librosa", db_path=state_db)
    assert len(stored) == len(RESCUE_PLAYBACK_TRACKS)

    by_filename = {
        Path(file_path).name: stable_id
        for stable_id, _title, file_path in rows
        if file_path is not None
    }
    for track in RESCUE_PLAYBACK_TRACKS:
        audio_path = data_dir / "fixture-audio" / track.filename
        assert audio_path.is_file()
        with wave.open(str(audio_path), "rb") as handle:
            assert handle.getnframes() > 0
            duration_s = handle.getnframes() / float(handle.getframerate())
        stable_id = by_filename[track.filename]
        row = next(item for item in stored if item["stable_id"] == stable_id)
        record = AnalysisRecord.from_json(row["record_json"])
        assert record.backend == "librosa"
        assert record.downbeats_s
        assert record.downbeats_s == sorted(record.downbeats_s)
        assert len(record.downbeats_s) == len(set(record.downbeats_s))
        assert all(
            record.downbeats_s[index] < record.downbeats_s[index + 1]
            for index in range(len(record.downbeats_s) - 1)
        )
        assert all(0.0 <= downbeat < duration_s for downbeat in record.downbeats_s)
        assert record.features_blob["downbeat_tracking"] is True

    fold_sid = by_filename[PERFORMANCE_FOLD_TRACK.filename]
    master_sid = by_filename[FIXTURE_TRACKS[0].filename]
    fold_record = AnalysisRecord.from_json(
        next(item for item in stored if item["stable_id"] == fold_sid)["record_json"]
    )
    master_record = AnalysisRecord.from_json(
        next(item for item in stored if item["stable_id"] == master_sid)["record_json"]
    )
    assert abs(fold_record.bpm - 64.0) < 4.0
    assert abs(master_record.bpm - 128.0) < 4.0
    assert not (data_dir / "analysis-pairs.json").exists()


@pytest.mark.requirement("LIBUX-18")
def test_rescue_playback_fixture_is_idempotent_on_second_build(tmp_path: Path) -> None:
    """[if] rescue build runs twice [then] row count stays 8 with no filler rows, [else stop]."""
    data_dir = tmp_path / "fixture-data"
    library_filenames = {track.filename for track in RESCUE_PLAYBACK_LIBRARY_TRACKS}
    first = build_rescue_playback(data_dir)
    second = build_rescue_playback(data_dir)
    assert len(first) == len(RESCUE_PLAYBACK_LIBRARY_TRACKS)
    assert len(second) == len(RESCUE_PLAYBACK_LIBRARY_TRACKS)
    first_ids = {stable_id for stable_id, _title, _path in first}
    second_ids = {stable_id for stable_id, _title, _path in second}
    assert first_ids == second_ids
    seen_paths: set[str] = set()
    for _stable_id, _title, file_path in second:
        assert file_path is not None
        path = Path(file_path)
        assert path.is_file()
        basename = path.name
        assert basename in library_filenames
        assert path.as_posix() not in seen_paths
        seen_paths.add(path.as_posix())
    state_db_path = data_dir / "state" / "state.db"
    conn = state_db.open_ro(state_db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL").fetchone()[0]
    finally:
        conn.close()
    assert count == len(RESCUE_PLAYBACK_LIBRARY_TRACKS)
    stored = fetch_records(
        stable_ids=list(second_ids),
        backend="librosa",
        db_path=state_db_path,
    )
    assert len(stored) == len(RESCUE_PLAYBACK_TRACKS)

    artwork_wav = data_dir / "fixture-audio" / RESCUE_PLAYBACK_ARTWORK_TRACK.filename
    expected_png = ARTWORK_SOURCE_PNG.read_bytes()
    assert hashlib.sha256(expected_png).hexdigest() == ARTWORK_PNG_SHA256
    artwork = audio_files.read_embedded_artwork(artwork_wav)
    assert artwork == (expected_png, "image/png")
    apics = _id3_apic_frames(artwork_wav)
    assert len(apics) == 1
    assert apics[0].type == 3
    assert apics[0].mime == "image/png"
    other_wavs = {
        track.filename
        for track in RESCUE_PLAYBACK_LIBRARY_TRACKS
        if track.filename != RESCUE_PLAYBACK_ARTWORK_TRACK.filename
    }
    for filename in other_wavs:
        path = data_dir / "fixture-audio" / filename
        assert audio_files.read_embedded_artwork(path) is None
