"""Every tunable of the rekordbox-ingestion helper, in one place.

Skill: (private path). Values here are decisions a
reader should see at a glance; each one carries the evidence it came from.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from apps.shared import platform_paths


@dataclass(frozen=True)
class Cfg:
    # One folder per batch lives here. Inside MUSIC_ROOTS (~/Music), so the
    # library tooling sees it; it is where the Oct 2026 batches were imported from.
    BATCH_ROOT: Path = platform_paths.HOME / "Music" / "_ingest"
    # The rekordbox playlist FOLDER that holds one blank playlist per batch.
    PLAYLIST_FOLDER: str = "_ingest"
    # Backups of the live DB set, kept forever, next to rekordbox's own backups.
    BACKUP_ROOT: Path = platform_paths.REKORDBOX_APP_DIR / "odj-backups"
    # Per-run JSON reports (counts, statuses, decisions) for later audit.
    REPORT_ROOT: Path = platform_paths.HOME / ".local" / "state" / "rekordbox-ingestion"
    # Files that make up the live DB set. master.db alone is NOT a backup: on
    # Thu 29 Oct 2026 the -wal was 4 MB and newer than master.db with rekordbox
    # closed, i.e. it held un-checkpointed rows a master.db-only copy loses.
    DB_SET_SUFFIXES: tuple[str, ...] = ("", "-wal", "-shm")
    PLAYLIST_XML_NAME: str = "masterPlaylists6.xml"
    # Process names of the rekordbox 7 bundle (Contents/MacOS + the agent app).
    # Matched EXACTLY and case-sensitively: `pgrep -x Rekordbox` misses the real
    # process, which is lowercase `rekordbox`.
    REKORDBOX_PROCESS_NAMES: frozenset[str] = frozenset(
        {"rekordbox", "rekordboxAgent", "rbinit", "rb_http_server", "edb_streamd"}
    )
    REKORDBOX_BUNDLE_ID: str = "com.pioneerdj.rekordboxdj"
    # Analysed code every fully analyzed track in the library carries: 10243 of
    # 10343 rows on Thu 29 Oct 2026, including the whole Sat 10 Oct batch.
    # Any other non-zero code counts as analyzed but is reported as unusual.
    LIBRARY_NORM_ANALYSED_CODE: int = 105
    # ANLZ files rekordbox 7 writes per analyzed track (share/PIONEER/USBANLZ).
    # .3EX only appears for some tracks (389 of 10243), so it is not required.
    REQUIRED_ANLZ_SUFFIXES: tuple[str, ...] = (".DAT", ".EXT", ".2EX")
    AUDIO_SUFFIXES: frozenset[str] = frozenset(
        {".mp3", ".wav", ".aif", ".aiff", ".flac", ".m4a", ".aac", ".ogg", ".alac", ".mp4"}
    )
    # Watch loop: poll cadence, give-up time, and the hands-off window before quit.
    POLL_SECONDS: int = 60
    TIMEOUT_MINUTES: int = 240
    IDLE_MINUTES_BEFORE_QUIT: int = 5
    QUIT_WAIT_SECONDS: int = 120
    # Consecutive snapshot reads that fail before the watch gives up.
    MAX_UNREADABLE_POLLS: int = 3


CFG = Cfg()
