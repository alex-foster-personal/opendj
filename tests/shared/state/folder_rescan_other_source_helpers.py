"""Shared helpers for the test_folder_rescan_other_source test modules (split from test_folder_rescan_other_source.py)."""

from __future__ import annotations

import os
from pathlib import Path

from apps.shared.state.ingest import folder_rescan
from apps.shared.state.writer import StateWriter

from .test_folder_rescan import _live_tracks, _write_wav, _write_wav_sample, _writer


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
    assert state_conn.execute("SELECT audio_hash FROM tracks WHERE stable_id = ?", (stable_id,)).fetchone()[0], (
        "the control: the refresh ran and wrote a hash"
    )
    return state_conn.execute("SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (stable_id,)).fetchone()[0]


def _serving(state_conn, stable_id: str) -> tuple[int, int]:
    """How many lanes of ``stable_id`` the canonical pointer serves, and how many are stale."""
    served = state_conn.execute("SELECT COUNT(*) FROM analysis_canonical WHERE stable_id = ?", (stable_id,)).fetchone()[
        0
    ]
    stale = state_conn.execute("SELECT COUNT(*) FROM analysis_stale WHERE stable_id = ?", (stable_id,)).fetchone()[0]
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
