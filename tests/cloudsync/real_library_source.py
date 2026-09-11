"""Where the real-library tier finds its source library, and how it copies it.

Two rules, both load-bearing:

1. **The default source is the packaged app's own library**,
   ``~/Library/Application Support/com.opendj.desktop/state/state.db``, the
   file the installed openDJ writes on this Mac. Every default this replaced
   sat under ``/Users/dev``, a home directory that exists on no current host,
   so the tier skipped everywhere and went green by measuring nothing (the
   "board with zero checks" case in ``.claude/rules/verification.md``).

2. **The source is opened READ-ONLY, once, to snapshot it.** The packaged app
   may be running and writing through WAL, so a byte copy of ``state.db``
   alone misses committed rows still sitting in ``state.db-wal``, and can
   tear mid-write. SQLite's online backup API over a ``mode=ro`` connection
   yields one consistent snapshot, and a ``mode=ro`` connection cannot write
   the source even by accident. Every later step (migration, repair,
   seeding) touches the snapshot only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: The library the installed openDJ app keeps on this machine. Absent on CI and
#: in cloud sessions, which is what keeps the tier's skip honest there.
PACKAGED_LIBRARY_STATE_DB: Path = (
    Path.home() / "Library" / "Application Support" / "com.opendj.desktop" / "state" / "state.db"
)


class LibraryPremiseMissing(AssertionError):
    """The library holds no unorderable stored stamp, so the tier would test nothing.

    Raised by ``real_library._require_legacy_stamps``. It lives in this leaf
    module so ``real_library_premise`` can catch it without an import cycle.
    """


def read_only_uri(path: Path) -> str:
    """A SQLite URI that opens ``path`` read-only (``mode=ro``)."""
    return f"{path.resolve().as_uri()}?mode=ro"


def snapshot_read_only(source: Path, target: Path) -> Path:
    """Copy ``source`` to ``target`` through SQLite's backup API.

    ``source`` is opened ``mode=ro`` and is never written. ``target`` must not
    exist yet: overwriting a file here would hide which snapshot a failing
    test actually read. SQLite errors propagate, so a snapshot that could not
    be taken never turns into an empty library.
    """
    if target.exists():
        raise FileExistsError(f"refusing to overwrite {target} with a snapshot of {source}")
    if not source.is_file():
        raise FileNotFoundError(f"no state.db to snapshot at {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(read_only_uri(source), uri=True)
    try:
        target_conn = sqlite3.connect(target)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        source_conn.close()
    return target


__all__ = [
    "PACKAGED_LIBRARY_STATE_DB",
    "LibraryPremiseMissing",
    "read_only_uri",
    "snapshot_read_only",
]
