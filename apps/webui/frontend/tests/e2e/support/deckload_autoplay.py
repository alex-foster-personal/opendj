from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter

from apps.webui.frontend.tests.e2e.support.deckload_fixture_analysis import _stable_ids_in_track_order
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
    AUTOPLAY_CHAIN_TRACK,
    FIXTURE_TRACKS,
    HUNT_CAMELOT_KEY,
    HUNT_PLAYLIST_A_ID,
    HUNT_PLAYLIST_A_NAME,
    HUNT_PLAYLIST_B_ID,
    HUNT_PLAYLIST_B_NAME,
    HUNT_TRACKS,
)

def build_autoplay_chain(
    data_dir: Path,
) -> list[tuple[str, str | None, str | None]]:
    """Extend ``build()``'s 2-track library with a 3rd, key/BPM-tagged track.

    Own function, own playlist (``AUTOPLAY_CHAIN_PLAYLIST_ID``): ``build()``
    itself, and the 2-track shape it produces, are untouched, so the 5 other
    e2e configs sharing this builder never see a 3rd track. Only the
    performance suite calls this.

    Tags land through the real ``StateWriter.set_field`` path with
    ``source="manual"`` -- the same call a manual tag edit makes in
    production (see ``apps/webui/server/sqlite_backend.py``'s PATCH
    /tracks/{stable_id} handler) -- never a direct SQLite write. Every track
    gets the SAME Camelot key and BPM, so AutoPlay's real compatibility gate
    (Camelot distance <= 1, BPM ratio inside phase-lock range) is satisfied
    at distance 0 / ratio 1.0 regardless of whatever pitch-range tolerance
    the page has active.
    """
    # Deliberately NOT `build(data_dir, seed_playlists=True)` followed by a
    # separate ingest for the chain file: once this has run once, the audio
    # dir holds 3 files while `build()`'s own `ensure_audio()` only ever
    # accounts for the 2-track `FIXTURE_TRACKS` tuple, so a second call to
    # plain `build()` on an already-chain-extended dir sees "3 rows for 2
    # files" and raises. Playwright re-imports this config more than once
    # per run (webServer startup + worker startup both evaluate it), so this
    # function must tolerate being called repeatedly against its OWN fully-
    # built output, not just against an empty dir. Folding FIXTURE_TRACKS and
    # the chain track into one list, ensured and ingested together, keeps
    # the "N rows for N files" invariant true no matter how many times this
    # runs against the same dir.
    _discard_stale_revision(data_dir)
    _reset_feedback_dir(data_dir)
    audio_dir = data_dir / AUDIO_SUBDIR
    generated_unsorted = (*FIXTURE_TRACKS, AUTOPLAY_CHAIN_TRACK)
    files = ensure_audio(audio_dir, generated_unsorted)
    state_db_path = data_dir / "state" / "state.db"
    rows = _ingest_and_verify(data_dir, audio_dir, files, "autoplay-chain")

    # Keeps the manifest's populated/empty playlist ids honest: `write_manifest`
    # always records them, so they must always actually exist in the DB, same
    # as the plain `build(..., seed_playlists=True)` path.
    _seed_playlists(state_db_path, rows)

    stable_ids = [stable_id for stable_id, _title, _file_path in rows]
    # `_track_rows` returns rows ORDER BY title, so line the generated tracks up
    # with them by filename rather than assuming the tuple order survived.
    by_filename = {track.filename: track for track in generated_unsorted}
    generated = [
        by_filename[Path(file_path).name]
        for _stable_id, _title, file_path in rows
        if file_path is not None and Path(file_path).name in by_filename
    ]
    now = datetime.now(UTC).isoformat()
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, actor="e2e-deckload-fixture")
    # Every track gets the SAME Camelot key, so AutoPlay's compatibility gate is
    # satisfied at distance 0. Each track keeps its OWN declared BPM: 124 and 128
    # are 1.032 apart, comfortably inside phase-lock range for AutoPlay, and
    # flattening them all to one value would destroy the distinct-BPM pair
    # `performance-controls.spec.ts::_realSyncPair` needs (it requires a ratio
    # differing by >= 0.005, so an all-128 library makes it throw
    # deterministically). Bot review, Wed 9 Sep 2026.
    bpm_by_stable_id = {
        stable_id: track.bpm
        for (stable_id, _title, file_path), track in zip(rows, generated, strict=True)
        if file_path is not None and Path(file_path).name == track.filename
    }
    if len(bpm_by_stable_id) != len(rows):
        raise SystemExit(
            "[ERROR] autoplay chain could not match every ingested row to its generated file"
        )
    try:
        for stable_id in stable_ids:
            writer.set_field(
                stable_id, "bpm", bpm_by_stable_id[stable_id],
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
        writer.set_playlist_memberships(AUTOPLAY_CHAIN_PLAYLIST_ID, stable_ids)
    finally:
        writer.close()
        conn.close()
    return rows



def build_autoplay_hunt(
    data_dir: Path,
) -> list[tuple[str, str | None, str | None]]:
    """Build a 6-track hunt library with two playlists. Does not call ``build()``.

    Own function, own playlist ids: ``build()`` itself, and the 2-track shape
    it produces, are untouched. Deliberately NOT ``build()`` followed by a
    second ingest -- once this has run once, the audio dir holds 6 files
    while ``build()``'s ``ensure_audio()`` only accounts for ``FIXTURE_TRACKS``,
    so a later plain ``build()`` on this dir would see "6 rows for 2 files".
    Playwright re-imports its config more than once per run, so this function
    must tolerate being called repeatedly against its own fully-built output.

    Playlist A is the 6 tracks in declared (HUNT_TRACKS) order; playlist B
    is the reverse membership of the same 6. Tags land through
    ``StateWriter.set_field`` with ``source="manual"``.
    """
    if len(HUNT_TRACKS) < 6:
        raise SystemExit("[ERROR] HUNT_TRACKS must contain at least 6 tracks")
    _discard_stale_revision(data_dir)
    _reset_feedback_dir(data_dir)
    audio_dir = data_dir / AUDIO_SUBDIR
    files = ensure_audio(audio_dir, HUNT_TRACKS)
    state_db_path = data_dir / "state" / "state.db"
    rows = _ingest_and_verify(data_dir, audio_dir, files, "autoplay-hunt")
    _seed_playlists(state_db_path, rows)

    ordered_ids = _stable_ids_in_track_order(rows, HUNT_TRACKS, "autoplay-hunt")
    bpm_by_stable_id = {
        stable_id: track.bpm for stable_id, track in zip(ordered_ids, HUNT_TRACKS, strict=True)
    }
    now = datetime.now(UTC).isoformat()
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, actor="e2e-deckload-fixture")
    try:
        for stable_id in ordered_ids:
            writer.set_field(
                stable_id, "bpm", bpm_by_stable_id[stable_id],
                source="manual", modified_at=now, confidence=1.0,
            )
            writer.set_field(
                stable_id, "key", HUNT_CAMELOT_KEY,
                source="manual", modified_at=now, confidence=1.0,
            )
        writer.insert_playlist(
            playlist_id=HUNT_PLAYLIST_A_ID,
            name=HUNT_PLAYLIST_A_NAME,
            vendor="fixture",
            vendor_pl_id=HUNT_PLAYLIST_A_ID,
        )
        writer.set_playlist_memberships(HUNT_PLAYLIST_A_ID, ordered_ids)
        writer.insert_playlist(
            playlist_id=HUNT_PLAYLIST_B_ID,
            name=HUNT_PLAYLIST_B_NAME,
            vendor="fixture",
            vendor_pl_id=HUNT_PLAYLIST_B_ID,
        )
        writer.set_playlist_memberships(HUNT_PLAYLIST_B_ID, list(reversed(ordered_ids)))
    finally:
        writer.close()
        conn.close()
    return rows
