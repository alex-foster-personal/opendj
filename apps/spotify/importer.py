"""End-to-end import orchestration for Phase 9 (CAT-01a).

Named ``importer`` (not ``import``) because ``import`` is a keyword.
CLI subcommand is still called ``import``.

Flow: fetch -> match -> write artifacts -> (live) write state.

Exit codes:
    0 -- success, all tracks matched.
    1 -- success, some tracks unmatched.
    2 -- Spotify API / network error.
    3 -- safety check failed.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .client import (
    SpotifyClient,
    SpotifyPlaylist,
)
from .matcher_adapter import MatchResult, load_local_tracks, match_spotify_tracks
from .reporting import ReportPaths, build_report_dir, write_all_reports
from .state_writer import (
    WriteSummary,
    backup_state_db,
    emit_reversal_script,
    open_state_rw_with_aux,
    write_playlist_and_pending,
)

__all__ = [
    "ImportRun",
    "run_import",
    "EXIT_OK",
    "EXIT_UNMATCHED",
    "EXIT_API_ERROR",
    "EXIT_SAFETY",
]

EXIT_OK: int = 0
EXIT_UNMATCHED: int = 1
EXIT_API_ERROR: int = 2
EXIT_SAFETY: int = 3


@dataclass
class ImportOptions:
    live: bool = False
    force: bool = False
    use_cache: bool = True
    max_tracks: int | None = None
    out_root: Path | None = None
    state_db_path: Path | None = None
    include_timestamp: bool = True
    progress: Callable[[str], None] | None = None


@dataclass
class ImportRun:
    playlist: SpotifyPlaylist
    result: MatchResult
    reports: ReportPaths
    write_summary: WriteSummary | None
    runtime_seconds: float
    live: bool


def run_import(
    playlist_id: str,
    *,
    client: SpotifyClient,
    options: ImportOptions | None = None,
) -> ImportRun:
    """Run a full Spotify import.

    ``client`` is injected so tests pass a fake. ``live=True`` requires
    the caller to have already validated the 6-rail safety pattern
    (typed confirmation etc.) -- the CLI wrapper enforces that.
    """
    opts = options or ImportOptions()
    progress = opts.progress or (lambda _msg: None)
    t0 = time.monotonic()

    progress(f"fetching playlist {playlist_id}")
    playlist = client.fetch_playlist(playlist_id, use_cache=opts.use_cache)
    if opts.max_tracks is not None and len(playlist.tracks) > opts.max_tracks:
        progress(
            f"cautious mode: truncating to first {opts.max_tracks} of "
            f"{len(playlist.tracks)} tracks"
        )
        playlist = SpotifyPlaylist(
            id=playlist.id,
            name=playlist.name,
            snapshot_id=playlist.snapshot_id,
            owner=playlist.owner,
            description=playlist.description,
            tracks=playlist.tracks[:opts.max_tracks],
        )

    progress("loading local tracks from state DB")
    from apps.shared.state import db as state_db

    try:
        ro = state_db.open_ro(opts.state_db_path)
        targets = load_local_tracks(ro)
        ro.close()
    except FileNotFoundError:
        progress("state DB missing; treating local library as empty")
        targets = []

    progress(f"matching {len(playlist.tracks)} tracks against {len(targets)} local")
    result = match_spotify_tracks(playlist.tracks, targets)

    progress("writing report artifacts")
    reports = build_report_dir(playlist.id, root=opts.out_root)
    write_all_reports(
        playlist, result, reports,
        runtime_seconds=time.monotonic() - t0,
        live=opts.live,
        include_timestamp=opts.include_timestamp,
    )

    write_summary: WriteSummary | None = None
    if opts.live:
        progress("LIVE mode: backing up state DB + writing playlist")
        backup = backup_state_db(opts.state_db_path)
        reversal = emit_reversal_script(backup, opts.state_db_path)
        conn = open_state_rw_with_aux(opts.state_db_path)
        try:
            write_summary = write_playlist_and_pending(
                conn,
                playlist,
                result,
                backup_path=backup,
                reversal_script_path=reversal,
                force=opts.force,
            )
        finally:
            conn.close()
        progress(f"backup: {backup}")
        progress(f"reversal script: {reversal}")

    runtime = time.monotonic() - t0
    return ImportRun(
        playlist=playlist,
        result=result,
        reports=reports,
        write_summary=write_summary,
        runtime_seconds=runtime,
        live=opts.live,
    )
