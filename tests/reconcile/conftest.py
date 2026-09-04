"""Shared real-library precondition for tests/reconcile.

Several tests in this package assert against the actual on-disk library
(``data/state/state.db``, or ``$MDT_DATA_DIR/state/state.db``) rather than a
synthetic fixture, and must skip cleanly when there is no real library to
point at. "No real library" means the db is absent, unreadable, or present
with a schema but zero track rows -- a bare-schema state.db (created by
booting the app once with nothing imported) is functionally the same "no
library here" condition as a missing file. See #1015: the previous guard
tested file presence only, so an empty-but-existing state.db passed the
guard and then failed the row-dependent assertions.

``HAS_REAL_LIBRARY`` and ``HAS_REAL_SCHEMA`` are resolved once at collection
time -- cheap (a single ``count(*)``). A MISSING file is "no library" (skip);
a file that exists but cannot be read (locked, corrupt, incompatible schema)
raises at collection, because that is a broken fixture, not an absent one.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from apps.shared import paths


def real_data_dir() -> Path:
    """The real (non-fixture) data dir: ``$MDT_DATA_DIR`` override, else the app default."""
    env = os.environ.get("MDT_DATA_DIR")
    return Path(env) if env else paths.DATA_DIR


REAL_STATE_DB: Path = real_data_dir() / "state" / "state.db"


def _real_track_count(state_db: Path) -> int | None:
    """``count(*) FROM tracks``; ``None`` when the db is absent (fresh checkout, CI).

    Any other failure RAISES. A locked, corrupt or incompatible-schema db must
    fail collection loudly, not read as "no library" and skip seven tests in an
    otherwise green run (PR #1022 thread r3923046732).
    """
    if not state_db.exists():
        return None
    try:
        with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True) as conn:
            (count,) = conn.execute("SELECT count(*) FROM tracks").fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"UNAVAILABLE: real library {state_db} exists but is unreadable ({exc}). "
            "Repair it or point MDT_DATA_DIR at a readable library; this is not a skip."
        ) from exc
    return count


_REAL_TRACKS: int | None = _real_track_count(REAL_STATE_DB)
# Two gates, because they answer different questions (thread r3923589515):
# the read-only-handle tests need only the migrated tables, so they run on an
# empty library; the row-dependent tests need real rows.
HAS_REAL_SCHEMA: bool = _REAL_TRACKS is not None
HAS_REAL_LIBRARY: bool = bool(_REAL_TRACKS)
