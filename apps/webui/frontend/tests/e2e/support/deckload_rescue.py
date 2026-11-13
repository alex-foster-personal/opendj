from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter

from apps.webui.frontend.tests.e2e.support.deckload_fixture_analysis import (
    _run_librosa_analysis,
    _run_own_beatgrid_analysis,
)
from apps.webui.frontend.tests.e2e.support.deckload_fixture_artwork import _ensure_rescue_artwork_embedded
from apps.webui.frontend.tests.e2e.support.deckload_fixture_audio import ensure_audio
from apps.webui.frontend.tests.e2e.support.deckload_fixture_core import (
    _discard_stale_revision,
    _ingest_and_verify,
    _reset_feedback_dir,
    _seed_playlists,
)
from apps.webui.frontend.tests.e2e.support.deckload_fixture_model import (
    AUDIO_SUBDIR,
    AUTOPLAY_CHAIN_CAMELOT_KEY,
    AUTOPLAY_CHAIN_PLAYLIST_ID,
    AUTOPLAY_CHAIN_PLAYLIST_NAME,
    RESCUE_PLAYBACK_LIBRARY_TRACKS,
    RESCUE_PLAYBACK_TRACKS,
)

def build_rescue_playback(
    data_dir: Path, *, own_beatgrid: bool = False
) -> list[tuple[str, str | None, str | None]]:
    """Build an analysis-backed performance rescue library for Beat Sync e2e.

    ``own_beatgrid`` additionally runs the production own beatgrid producer
    (see :func:`_run_own_beatgrid_analysis`), which is what gives the decks a
    grid at all since #3561.
    """
    _discard_stale_revision(data_dir)
    _reset_feedback_dir(data_dir)
    audio_dir = data_dir / AUDIO_SUBDIR
    files = ensure_audio(audio_dir, RESCUE_PLAYBACK_LIBRARY_TRACKS)
    _ensure_rescue_artwork_embedded(audio_dir)
    state_db_path = data_dir / "state" / "state.db"
    rows = _ingest_and_verify(data_dir, audio_dir, files, "rescue-playback")
    _seed_playlists(state_db_path, rows)

    transport_filenames = {track.filename for track in RESCUE_PLAYBACK_TRACKS}
    autoplay_stable_ids = [
        stable_id
        for stable_id, _title, file_path in rows
        if file_path and Path(file_path).name in transport_filenames
    ]
    if len(autoplay_stable_ids) != len(RESCUE_PLAYBACK_TRACKS):
        raise SystemExit(
            f"[ERROR] rescue-playback expected {len(RESCUE_PLAYBACK_TRACKS)} autoplay-chain "
            f"members, got {len(autoplay_stable_ids)}"
        )
    by_filename = {track.filename: track for track in RESCUE_PLAYBACK_LIBRARY_TRACKS}
    now = datetime.now(UTC).isoformat()
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, actor="e2e-deckload-fixture")
    bpm_by_stable_id: dict[str, float] = {}
    for stable_id, _title, file_path in rows:
        if file_path is None:
            raise SystemExit(f"[ERROR] rescue-playback row {stable_id!r} has no file_path")
        track = by_filename.get(Path(file_path).name)
        if track is None:
            raise SystemExit(
                f"[ERROR] rescue-playback ingested unknown file {Path(file_path).name!r}"
            )
        bpm_by_stable_id[stable_id] = track.bpm
    if len(bpm_by_stable_id) != len(rows):
        raise SystemExit(
            "[ERROR] rescue-playback could not match every ingested row to its generated file"
        )
    try:
        for stable_id, bpm in bpm_by_stable_id.items():
            writer.set_field(
                stable_id, "bpm", bpm,
                source="manual", modified_at=now, confidence=1.0,
            )
            writer.set_field(
                stable_id, "key", AUTOPLAY_CHAIN_CAMELOT_KEY,
                source="manual", modified_at=now, confidence=1.0,
            )
        writer.insert_playlist(
            playlist_id=AUTOPLAY_CHAIN_PLAYLIST_ID,
            name=AUTOPLAY_CHAIN_PLAYLIST_NAME,
            vendor="fixture",
            vendor_pl_id=AUTOPLAY_CHAIN_PLAYLIST_ID,
        )
        writer.set_playlist_memberships(AUTOPLAY_CHAIN_PLAYLIST_ID, autoplay_stable_ids)
    finally:
        writer.close()
        conn.close()

    _run_librosa_analysis(data_dir, rows, RESCUE_PLAYBACK_TRACKS, "rescue-playback")
    if own_beatgrid:
        _run_own_beatgrid_analysis(data_dir, rows, RESCUE_PLAYBACK_TRACKS, "rescue-playback")
    return rows

