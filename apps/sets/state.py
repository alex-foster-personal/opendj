"""Per-set SQLite shim (session + event timeline).

Plan 12-01 Step 1. The shim intentionally lives in its own file
(``data/sets/sets.db``) so Phase 12 can ship without a hard dep on
Phase 5's ``apps.shared.state``. Event shape is per CONTEXT D3:

    {session_id, timestamp_s, wall_clock, deck, track_stable_id,
     action, value, source}

If Phase 5 later adds a compatible ``sets`` table to ``state.db`` we
migrate the rows over via a follow-up plan; the API here is chosen to
match what Phase 5 will expose (``record_event`` / ``fetch_events``).

Every write goes through :class:`SetsState`. Tests use the
``SETS_DB`` path override for hermetic runs.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import paths as sets_paths

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sets (
        session_id     TEXT PRIMARY KEY,
        started_at     TEXT NOT NULL,
        ended_at       TEXT,
        capture_device TEXT NOT NULL,
        share_state    TEXT NOT NULL DEFAULT 'private'
                          CHECK (share_state IN ('private','shared_local','shared_cloud')),
        notes          TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id      TEXT NOT NULL REFERENCES sets(session_id) ON DELETE CASCADE,
        timestamp_s     REAL NOT NULL,
        wall_clock      TEXT NOT NULL,
        deck            TEXT,
        track_stable_id TEXT,
        action          TEXT NOT NULL,
        value_json      TEXT,
        source          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, timestamp_s)",
)


# ---------------------------------------------------------------------------
# domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRow:
    """Row from the ``sets`` table."""

    session_id: str
    started_at: str
    ended_at: str | None
    capture_device: str
    share_state: str
    notes: str | None


@dataclass(frozen=True)
class Event:
    """One timeline event (mirrored between JSONL + events table)."""

    session_id: str
    timestamp_s: float
    wall_clock: str
    action: str
    source: str
    deck: str | None = None
    track_stable_id: str | None = None
    value: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise as the JSONL canonical shape."""
        return asdict(self)


# ---------------------------------------------------------------------------
# state helper
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA_SQL:
        conn.execute(stmt)


class SetsState:
    """Thread-safe writer over the shim DB.

    Creates its own connection per call; SQLite handles cross-thread work
    via ``check_same_thread=False`` and a lock. This mirrors the pattern
    from ``apps.shared.state.db`` but with a lighter surface.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path: Path = Path(db_path) if db_path is not None else sets_paths.SETS_DB
        self._lock = threading.Lock()
        self._ensured = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if not self._ensured:
            _ensure_schema(conn)
            self._ensured = True
        return conn

    @contextmanager
    def _rw(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                yield conn
            finally:
                conn.close()

    # --- sessions ------------------------------------------------------

    def open_session(
        self,
        session_id: str,
        *,
        capture_device: str,
        started_at: str | None = None,
        notes: str | None = None,
    ) -> SessionRow:
        """Insert a new ``sets`` row; ``share_state`` defaults to ``private``.

        Raises :class:`sqlite3.IntegrityError` if ``session_id`` already
        exists. The caller owns collision handling (see
        ``record.resolve_session_id``).
        """
        started = started_at or _now_iso()
        with self._rw() as conn:
            conn.execute(
                """
                INSERT INTO sets(session_id, started_at, capture_device, share_state, notes)
                VALUES (?, ?, ?, 'private', ?)
                """,
                (session_id, started, capture_device, notes),
            )
        return SessionRow(
            session_id=session_id,
            started_at=started,
            ended_at=None,
            capture_device=capture_device,
            share_state="private",
            notes=notes,
        )

    def end_session(self, session_id: str, *, ended_at: str | None = None) -> None:
        """Stamp ``ended_at`` on an existing session."""
        stamp = ended_at or _now_iso()
        with self._rw() as conn:
            conn.execute(
                "UPDATE sets SET ended_at = ? WHERE session_id = ?",
                (stamp, session_id),
            )

    def get_session(self, session_id: str) -> SessionRow | None:
        with self._rw() as conn:
            row = conn.execute(
                """
                SELECT session_id, started_at, ended_at, capture_device,
                       share_state, notes
                FROM sets WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return SessionRow(*row)

    def list_sessions(self) -> list[SessionRow]:
        with self._rw() as conn:
            rows = conn.execute(
                """
                SELECT session_id, started_at, ended_at, capture_device,
                       share_state, notes
                FROM sets ORDER BY started_at DESC
                """
            ).fetchall()
        return [SessionRow(*r) for r in rows]

    # --- events --------------------------------------------------------

    def record_event(self, event: Event) -> int:
        """Append a timeline event to the ``events`` table.

        Returns the new row id.
        """
        with self._rw() as conn:
            cur = conn.execute(
                """
                INSERT INTO events(session_id, timestamp_s, wall_clock, deck,
                                   track_stable_id, action, value_json, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.session_id,
                    event.timestamp_s,
                    event.wall_clock,
                    event.deck,
                    event.track_stable_id,
                    event.action,
                    json.dumps(event.value, separators=(",", ":")) if event.value else None,
                    event.source,
                ),
            )
            return int(cur.lastrowid or 0)

    def fetch_events(
        self,
        session_id: str,
        *,
        since_s: float | None = None,
        action: str | None = None,
    ) -> list[Event]:
        """Return events for ``session_id`` in chronological order."""
        sql = [
            """
            SELECT session_id, timestamp_s, wall_clock, deck, track_stable_id,
                   action, value_json, source
            FROM events WHERE session_id = ?
            """
        ]
        args: list[Any] = [session_id]
        if since_s is not None:
            sql.append("AND timestamp_s >= ?")
            args.append(since_s)
        if action is not None:
            sql.append("AND action = ?")
            args.append(action)
        sql.append("ORDER BY timestamp_s ASC, id ASC")
        with self._rw() as conn:
            rows = conn.execute(" ".join(sql), args).fetchall()
        return [_row_to_event(r) for r in rows]

    def count_events(self, session_id: str) -> int:
        with self._rw() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row[0] if row else 0)


def _row_to_event(row: tuple[Any, ...]) -> Event:
    (session_id, timestamp_s, wall_clock, deck, track_stable_id,
     action, value_json, source) = row
    return Event(
        session_id=session_id,
        timestamp_s=float(timestamp_s),
        wall_clock=wall_clock,
        deck=deck,
        track_stable_id=track_stable_id,
        action=action,
        value=json.loads(value_json) if value_json else {},
        source=source,
    )


__all__ = [
    "SCHEMA_SQL",
    "SessionRow",
    "Event",
    "SetsState",
]
