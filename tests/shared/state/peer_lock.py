"""A real SQLite peer holding the state.db FILE lock, for busy-wait tests.

The lock is EXCLUSIVE on the database file, which is what a closing last
connection holds while it checkpoints and deletes -wal/-shm. In WAL mode that
(and wal-index recovery) is the only thing that blocks a reader, and it is
what a concurrent boot met on the Sat 26 Sep 2026 trunk red. A real SQLite
connection in EXCLUSIVE locking mode takes it; nothing is emulated.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from apps.shared.state import db as state_db
from tests.webui.pre_v7_state import build_pre_v7_state_db

_EXCLUSIVE_LOCK_HOLDER = """
import sqlite3
import sys
import time

conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA locking_mode = EXCLUSIVE")
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE schema_meta SET applied_at = applied_at WHERE version = 1")
conn.execute("COMMIT")
print("held", flush=True)
time.sleep(float(sys.argv[2]))
conn.close()
"""


def hold_exclusive_lock(db_path: Path, hold_s: float) -> subprocess.Popen[str]:
    """Start a peer process holding ``db_path`` EXCLUSIVE for ``hold_s`` seconds.

    Returns once the lock is held, so the caller's next open meets it.
    """
    holder = subprocess.Popen(
        [sys.executable, "-c", _EXCLUSIVE_LOCK_HOLDER, str(db_path), str(hold_s)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    line = holder.stdout.readline().strip()
    if line != "held":
        holder.kill()
        raise AssertionError(f"lock holder never took the lock: {holder.communicate()}")
    return holder


def current_wal_state_db(db_path: Path) -> Path:
    """A state.db at SCHEMA_VERSION in WAL mode, as every app-written db is."""
    build_pre_v7_state_db(db_path)
    state_db.open_rw(db_path).close()
    return db_path
