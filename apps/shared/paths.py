"""Canonical paths for the _sync-tools project.

Central module for every file/path constant used across the codebase so we
never sprinkle hard-coded paths around. Also exposes `copy_live_dbs()` which
snapshots the live Rekordbox + djay databases into ``data/`` for safe,
read-only analysis.

Every OS-branching decision (rekordbox app dir, MUSIC_ROOTS, the Mac<->
Windows path map) lives in :mod:`apps.shared.platform_paths`; this module
imports those values and re-exports them so existing
``from apps.shared.paths import ...`` call sites keep working unchanged.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from apps.shared.platform_paths import (
    DATA_DIR,
    DJAY_LIVE_DB,
    HOME,
    MUSIC_ROOTS,
    PROJECT_ROOT,
    REKORDBOX_LIVE_DB,
    # Private, so it is absent from __all__ and reads as unused to F401, but
    # tests/shared/test_paths.py reaches it through this module. Deleting the
    # re-export breaks that suite.
    _parse_music_roots,  # noqa: F401
)

__all__ = [
    "AUDIO_EXTENSIONS",
    "DATA_DIR",
    "DEDUP_ARCHIVE_ROOT",
    "DEDUP_CLUSTERS_CSV",
    "DEDUP_DIR",
    "DEDUP_FALLBACK_DB",
    "DEDUP_MANUAL_REVIEW_CSV",
    "DEDUP_REWRITE_PLAN_CSV",
    "DEDUP_REWRITE_SUMMARY_MD",
    "DJAY_LIVE_DB",
    "DJAY_WORKING_DB",
    "HOME",
    "MUSIC_ROOTS",
    "PROJECT_ROOT",
    "REKORDBOX_LIVE_DB",
    "REKORDBOX_PLAIN_DB",
    "REKORDBOX_WORKING_DB",
    "STATE_AUTHORITATIVE_BACKUP_DIR",
    "STATE_DB",
    "STATE_DIR",
    "TAGS_BACKUPS_DIR",
    "TAGS_DIR",
    "TAGS_REVERSAL_DIR",
    "TAGS_UNIFIED_PREVIEW_CSV",
    "copy_live_dbs",
]

# ``DATA_DIR`` is re-exported from :mod:`apps.shared.platform_paths`, which is
# the single place ``MDT_DATA_DIR`` is honored. Recomputing it here as
# ``PROJECT_ROOT / "data"`` made this module the one path family that ignored
# that override, so a worktree backend pointed at the primary checkout's data
# still resolved STATE_DB to its own empty ``data/``. Unset MDT_DATA_DIR keeps
# the identical default.

# ----- Rekordbox ---------------------------------------------------------
# Byte-for-byte snapshot of the live master.db, so it is still
# SQLCipher-encrypted and no plain sqlite3 client can read it.
REKORDBOX_WORKING_DB: Path = DATA_DIR / "master.db.copy"
# The decrypted working copy every reader in this repo actually consumes.
# Static by convention: refresh means re-decrypt (see
# ``apps.shared.rekordbox_db.ensure_plain_db``) and then
# ``rm -rf data/state/anlz-cache/``.
REKORDBOX_PLAIN_DB: Path = DATA_DIR / "master.plain.db"

# ----- djay Pro ----------------------------------------------------------
DJAY_WORKING_DB: Path = DATA_DIR / "djay_MediaLibrary.db.copy"

# ----- Shared-state layer (Phase 5) --------------------------------------
# Local canonical projection. Derivative of vendor DBs; safe to delete.
# Always gitignored under ``data/``. Phase 11 (Litestream) may relocate.
STATE_DIR: Path = DATA_DIR / "state"
STATE_DB: Path = STATE_DIR / "state.db"
STATE_AUTHORITATIVE_BACKUP_DIR: Path = DATA_DIR / "backup" / "state-authoritative"

# ----- Phase 7 dedup + tag unification ----------------------------------
# Fallback SQLite store used when the Phase 5 shared-state DB is not yet
# ready (or when callers want a dedicated dedup workspace). The same
# schema works either way; migrating into state.db is a cheap chore.
DEDUP_DIR: Path = DATA_DIR / "dedup"
DEDUP_FALLBACK_DB: Path = DEDUP_DIR / "phase7.sqlite"
DEDUP_CLUSTERS_CSV: Path = DEDUP_DIR / "clusters.csv"
DEDUP_MANUAL_REVIEW_CSV: Path = DEDUP_DIR / "manual-review.csv"
DEDUP_REWRITE_PLAN_CSV: Path = DEDUP_DIR / "rewrite-plan.csv"
DEDUP_REWRITE_SUMMARY_MD: Path = DEDUP_DIR / "rewrite-summary.md"
DEDUP_ARCHIVE_ROOT: Path = DEDUP_DIR / "archived"

TAGS_DIR: Path = DATA_DIR / "tags"
TAGS_BACKUPS_DIR: Path = TAGS_DIR / "backups"
TAGS_UNIFIED_PREVIEW_CSV: Path = TAGS_DIR / "unified-preview.csv"
TAGS_REVERSAL_DIR: Path = TAGS_DIR / "reversals"

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
