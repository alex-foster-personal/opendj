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
    DJAY_LIVE_DB,
    HOME,
    MUSIC_ROOTS,
    PROJECT_ROOT,
    REKORDBOX_LIVE_DB,
    _parse_music_roots,
)

__all__ = [
    "HOME",
    "PROJECT_ROOT",
    "DATA_DIR",
    "REKORDBOX_LIVE_DB",
    "REKORDBOX_WORKING_DB",
    "DJAY_LIVE_DB",
    "DJAY_WORKING_DB",
    "STATE_DIR",
    "STATE_DB",
    "MUSIC_ROOTS",
    "DEDUP_DIR",
    "DEDUP_FALLBACK_DB",
    "DEDUP_CLUSTERS_CSV",
    "DEDUP_MANUAL_REVIEW_CSV",
    "DEDUP_REWRITE_PLAN_CSV",
    "DEDUP_REWRITE_SUMMARY_MD",
    "DEDUP_ARCHIVE_ROOT",
    "TAGS_DIR",
    "TAGS_BACKUPS_DIR",
    "TAGS_UNIFIED_PREVIEW_CSV",
    "TAGS_REVERSAL_DIR",
    "AUDIO_EXTENSIONS",
    "copy_live_dbs",
]

DATA_DIR: Path = PROJECT_ROOT / "data"

# ----- Rekordbox ---------------------------------------------------------
REKORDBOX_WORKING_DB: Path = DATA_DIR / "master.db.copy"

# ----- djay Pro ----------------------------------------------------------
DJAY_WORKING_DB: Path = DATA_DIR / "djay_MediaLibrary.db.copy"

# ----- Shared-state layer (Phase 5) --------------------------------------
# Local canonical projection. Derivative of vendor DBs; safe to delete.
# Always gitignored under ``data/``. Phase 11 (Litestream) may relocate.
STATE_DIR: Path = DATA_DIR / "state"
STATE_DB: Path = STATE_DIR / "state.db"

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
