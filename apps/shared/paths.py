"""Canonical paths for the _sync-tools project.

Central module for every file/path constant used across the codebase so we
never sprinkle hard-coded paths around. Also exposes `copy_live_dbs()` which
snapshots the live Rekordbox + djay databases into ``data/`` for safe,
read-only analysis.
"""
from __future__ import annotations

import shutil
from pathlib import Path

HOME: Path = Path.home()

# Project root is the directory that contains ``apps/``. This file lives at
# ``<project>/apps/shared/paths.py`` → parents[2] is the project root.
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DATA_DIR: Path = PROJECT_ROOT / "data"

# ----- Rekordbox ---------------------------------------------------------
REKORDBOX_LIVE_DB: Path = HOME / "Library" / "Pioneer" / "rekordbox" / "master.db"
REKORDBOX_WORKING_DB: Path = DATA_DIR / "master.db.copy"

# ----- djay Pro ----------------------------------------------------------
DJAY_LIVE_DB: Path = (
    HOME / "Music" / "djay" / "djay Media Library.djayMediaLibrary" / "MediaLibrary.db"
)
DJAY_WORKING_DB: Path = DATA_DIR / "djay_MediaLibrary.db.copy"

# ----- Shared-state layer (Phase 5) --------------------------------------
# Local canonical projection. Derivative of vendor DBs; safe to delete.
# Always gitignored under ``data/``. Phase 11 (Litestream) may relocate.
STATE_DIR: Path = DATA_DIR / "state"
STATE_DB: Path = STATE_DIR / "state.db"

# ----- Filesystem music library -----------------------------------------
# We scan the whole ``~/Music`` tree so comparison catches files outside the
# curated "Manual Library" subfolder too.
MUSIC_ROOTS: list[Path] = [Path("/Users/dev3/Music")]

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".m4a", ".aac", ".wav", ".aiff", ".aif", ".flac", ".ogg", ".alac"}
)


def copy_live_dbs() -> dict[str, Path | None]:
    """Snapshot the live Rekordbox + djay DBs into ``data/``.

    Idempotent: safe to call repeatedly; uses ``shutil.copy2`` so mtime is
    preserved and we always overwrite the previous working copy. The djay DB
    is optional — if it's missing we return ``None`` for that key instead of
    raising.

    Returns
    -------
    dict
        ``{"rekordbox": Path | None, "djay": Path | None}`` — ``None`` means
        the live DB didn't exist at all.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path | None] = {"rekordbox": None, "djay": None}

    if REKORDBOX_LIVE_DB.exists():
        shutil.copy2(REKORDBOX_LIVE_DB, REKORDBOX_WORKING_DB)
        result["rekordbox"] = REKORDBOX_WORKING_DB
    # else: leave as None; caller decides whether to raise.

    try:
        if DJAY_LIVE_DB.exists():
            shutil.copy2(DJAY_LIVE_DB, DJAY_WORKING_DB)
            result["djay"] = DJAY_WORKING_DB
    except FileNotFoundError:
        # Race between exists() and copy — treat as "not present".
        result["djay"] = None

    return result
