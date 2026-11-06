"""Unit tests for deckload fixture BPM validation (PRRT_kwDOSEvNd860UWg3)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.analysis.record import AnalysisRecord
from apps.webui.frontend.tests.e2e.support import cmdk_preview_cue_fixture
from apps.webui.frontend.tests.e2e.support.deckload_fixture import (
    FIXTURE_TRACKS,
    PERFORMANCE_FOLD_TRACK,
    RESCUE_PLAYBACK_TRACKS,
    FixtureTrack,
    _assert_fixture_bpms,
)


def _record(stable_id: str, bpm: float) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=stable_id,
        backend="librosa",
        backend_version="librosa==0.10.2",
        analyzed_at=datetime(2026, 4, 17, 1, 23, 45, tzinfo=UTC),
        duration_s=60.0,
        sample_rate=44100,
        bpm=bpm,
        bpm_confidence=0.95,
        key_camelot="8A",
        key_openkey="1m",
        key_confidence=0.88,
        energy=7,
        onsets_s=[0.5, 1.0],
        downbeats_s=[0.0],
        rms_peaks_s=[1.2],
        features_blob={"downbeat_tracking": True},
    )


def _fixture_rows_and_stored(
    tmp_path: Path,
    tracks: tuple[FixtureTrack, ...],
    bpms: dict[str, float],
) -> tuple[list[tuple[str, str | None, str | None]], list[dict[str, str]]]:
    rows: list[tuple[str, str | None, str | None]] = []
    stored: list[dict[str, str]] = []
    for index, track in enumerate(tracks):
        stable_id = f"pathid_{index}_{track.filename.replace('.', '_')}"
        file_path = str(tmp_path / track.filename)
        rows.append((stable_id, track.filename, file_path))
        stored.append(
            {
                "stable_id": stable_id,
                "record_json": _record(stable_id, bpms[track.filename]).to_json(),
            }
        )
    return rows, stored


@pytest.mark.requirement("CMDK-08")
def test_assert_fixture_bpms_accepts_cmdk_preview_cue_tracks(tmp_path: Path) -> None:
    """[if] cmdk TRACKS match stored BPMs [then] checker passes, [else stop]."""
    tracks = cmdk_preview_cue_fixture.TRACKS
    bpms = {track.filename: track.bpm for track in tracks}
    rows, stored = _fixture_rows_and_stored(tmp_path, tracks, bpms)
    _assert_fixture_bpms(rows, stored, tracks, "cmdk-preview-cue")


@pytest.mark.requirement("CMDK-08")
def test_assert_fixture_bpms_rejects_cmdk_track_with_wrong_bpm(tmp_path: Path) -> None:
    """[if] a cmdk track BPM is off by more than 4 [then] SystemExit names file, [else stop]."""
    tracks = cmdk_preview_cue_fixture.TRACKS
    bpms = {track.filename: track.bpm for track in tracks}
    bpms["cmdk-preview-cue-a-128bpm.wav"] = 200.0
    rows, stored = _fixture_rows_and_stored(tmp_path, tracks, bpms)
    with pytest.raises(SystemExit, match=r"cmdk-preview-cue-a-128bpm\.wav"):
        _assert_fixture_bpms(rows, stored, tracks, "cmdk-preview-cue")


@pytest.mark.requirement("CMDK-08")
def test_assert_fixture_bpms_checks_rescue_playback_fold_and_master(tmp_path: Path) -> None:
    """[if] rescue tracks match BPMs [then] checker passes, [else stop]."""
    tracks = RESCUE_PLAYBACK_TRACKS
    bpms = {track.filename: track.bpm for track in tracks}
    rows, stored = _fixture_rows_and_stored(tmp_path, tracks, bpms)
    _assert_fixture_bpms(rows, stored, tracks, "rescue-playback")


@pytest.mark.requirement("CMDK-08")
def test_assert_fixture_bpms_rejects_rescue_fold_with_wrong_bpm(tmp_path: Path) -> None:
    """[if] fold BPM is off by more than 4 [then] SystemExit names fold file, [else stop]."""
    tracks = (PERFORMANCE_FOLD_TRACK, FIXTURE_TRACKS[0])
    bpms = {
        PERFORMANCE_FOLD_TRACK.filename: 90.0,
        FIXTURE_TRACKS[0].filename: FIXTURE_TRACKS[0].bpm,
    }
    rows, stored = _fixture_rows_and_stored(tmp_path, tracks, bpms)
    with pytest.raises(SystemExit, match=r"webkit-fixture-d-64bpm-fold\.wav"):
        _assert_fixture_bpms(rows, stored, tracks, "rescue-playback")
