"""Explicit, warned SQLite optimize for one named library data directory (LIBM-126, issue #12332).

``optimize_library_db(data_dir)`` runs ``PRAGMA optimize`` then ``VACUUM`` on
``<data_dir>/state/state.db`` and reports sizes and elapsed time. It never runs as a side
effect: only ``POST /api/v1/library/maintenance/optimize`` and ``opendj library optimize``
call it, and both refuse without an explicit confirmation. VACUUM rewrites the whole file
and holds the writer lock while it runs, so the callers warn before they start.

Requirements:
- ✔︎ ✅ 🎯 [if] the data directory has no state.db [then ⛔️] OptimizeRefused LIBRARY_DB_MISSING, no file is created
- ✔︎ ✅ 🎯 [if] state.db has no library schema [then ⛔️] OptimizeRefused NOT_A_LIBRARY, nothing is vacuumed
- ✔︎ ✅ 🎯 [if] an initialized library is optimized [then ⛔️] a result with the db path, sizes and elapsed time
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from apps.shared.state.db import open_rw

STATE_DB_RELATIVE = Path("state") / "state.db"
WARNING = (
    "VACUUM rewrites the whole library database and blocks every writer until it finishes; run it with the app idle."
)


class OptimizeRefused(Exception):
    """The named data directory is not an optimizable library; ``code`` is the stable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OptimizeResult:
    db_path: Path
    bytes_before: int
    bytes_after: int
    elapsed_ms: int


def library_db_path(data_dir: Path) -> Path:
    """The state DB inside an explicitly named data directory."""
    return data_dir / STATE_DB_RELATIVE


def optimize_library_db(data_dir: Path) -> OptimizeResult:
    """Run PRAGMA optimize and VACUUM on ``data_dir``'s state DB; raise OptimizeRefused or sqlite3.Error."""
    db_path = library_db_path(data_dir)
    if not db_path.is_file():
        raise OptimizeRefused("LIBRARY_DB_MISSING", f"no library database at {db_path}")
    bytes_before = db_path.stat().st_size
    started = time.monotonic()
    conn = open_rw(db_path, apply_schema=False)
    try:
        has_tracks = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tracks'").fetchone()
        if has_tracks is None:
            raise OptimizeRefused("NOT_A_LIBRARY", f"{db_path} has no library schema (no tracks table)")
        conn.execute("PRAGMA optimize")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return OptimizeResult(
        db_path=db_path,
        bytes_before=bytes_before,
        bytes_after=db_path.stat().st_size,
        elapsed_ms=round((time.monotonic() - started) * 1000),
    )


__all__ = [
    "STATE_DB_RELATIVE",
    "WARNING",
    "OptimizeRefused",
    "OptimizeResult",
    "library_db_path",
    "optimize_library_db",
]
