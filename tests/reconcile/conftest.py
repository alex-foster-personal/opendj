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
from apps.shared.state import schema as state_schema


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
            (version,) = conn.execute("SELECT max(version) FROM schema_meta").fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"UNAVAILABLE: real library {state_db} exists but is unreadable ({exc}). "
            "Repair it or point MDT_DATA_DIR at a readable library; this is not a skip."
        ) from exc
    _require_current_schema(state_db, version)
    return count



def _require_current_schema(state_db: Path, version: int | None) -> None:
    """Raise unless ``state_db`` is migrated to the schema this code expects.

    The row count above answers "is there a library", not "can these tests
    read it". A library left at an older ``SCHEMA_VERSION`` answers the first
    question yes, passes the guard, and then dies deep inside a test on a
    column its migration never added. Measured Wed 9 Sep 2026: a v5 library
    against v8 code fails with ``no such column: deleted_at`` from
    ``apps/reconcile/reacquire.py``, which reads as a broken test rather than
    a stale fixture.

    That is the defect this module already removed, one level up: a check
    passing because it asked an easier question than the one that matters.
    The module docstring promised an "incompatible schema" fails collection
    loudly; this is what makes that true.
    """
    if version == state_schema.SCHEMA_VERSION:
        return
    raise RuntimeError(
        f"UNAVAILABLE: real library {state_db} is at schema v{version}, but this "
        f"code expects v{state_schema.SCHEMA_VERSION}. Migrate it once by opening "
        "it with apps.shared.state.db.open_rw, or point MDT_DATA_DIR at a current "
        "library; this is not a skip."
    )


_REAL_TRACKS: int | None = _real_track_count(REAL_STATE_DB)
# Two gates, because they answer different questions (thread r3923589515):
# the read-only-handle tests need only the migrated tables, so they run on an
# empty library; the row-dependent tests need real rows.
HAS_REAL_SCHEMA: bool = _REAL_TRACKS is not None
HAS_REAL_LIBRARY: bool = bool(_REAL_TRACKS)
