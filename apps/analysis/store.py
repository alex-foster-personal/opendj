"""Analysis persistence bridge to the Phase 5 state layer.

Wires :mod:`apps.analysis` writes to the shared ``state.db`` file via
:mod:`apps.shared.state.db` and publishes ``analyze`` events through
:class:`apps.shared.state.events.EventBus`. The previous
``apps.shared.state.analysis_shim`` module has been retired; its logic
now lives here as the owning module for the analysis domain tables.

Rationale for keeping the ``analysis`` + ``analysis_events`` tables
rather than folding entirely into Phase 5's ``events`` table:

* Phase 5 models identity/provenance/playlists but does not (yet) model
  analysis records. Phase 6 tests and downstream tooling read the
  ``analysis`` + ``analysis_events`` tables by name.
* The additive DDL runs on the same SQLite file (``STATE_DB``) so the
  durable event rows Phase 5 writes to its ``events`` table and the
  analysis-specific rows here share a connection, WAL journal, and
  PRAGMAs (via :func:`apps.shared.state.db.open_rw`).
* Every successful insert/update also publishes a canonical
  ``analyze`` :class:`apps.shared.state.types.Event` on the shared
  in-process :class:`EventBus`, so Phase 6 consumers registered on the
  bus see the same signal they would get from :class:`StateWriter`.

The public signatures (``upsert_record``, ``publish_event``,
``fetch_records_by_ids``, plus the legacy helpers ``open_conn``,
``upsert``, ``publish``, ``fetch_records`` re-exported for the two
CLIs that used the old shim directly) are unchanged.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from apps.shared.state import db as state_db
from apps.shared.state.events import EventBus
from apps.shared.state.types import Event

from .record import AnalysisRecord

# --- additive schema ----------------------------------------------------

_ANALYSIS_TABLES_SQL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS analysis (
        stable_id        TEXT NOT NULL,
        backend          TEXT NOT NULL,
        backend_version  TEXT NOT NULL,
        analyzed_at      TEXT NOT NULL,
        duration_s       REAL NOT NULL,
        sample_rate      INTEGER NOT NULL,
        bpm              REAL NOT NULL,
        bpm_confidence   REAL NOT NULL,
        key_camelot      TEXT NOT NULL,
        key_openkey      TEXT NOT NULL,
        key_confidence   REAL NOT NULL,
        energy           INTEGER NOT NULL,
        energy_source    TEXT NOT NULL,
        record_json      TEXT NOT NULL,
        PRIMARY KEY (stable_id, backend, backend_version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_analysis_stable_id ON analysis(stable_id)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_backend   ON analysis(backend)",
    """
    CREATE TABLE IF NOT EXISTS analysis_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,
        event_type   TEXT NOT NULL,
        stable_id    TEXT,
        payload_json TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_analysis_events_type   ON analysis_events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_events_stable ON analysis_events(stable_id)",
]


def _ensure_analysis_tables(conn: sqlite3.Connection) -> None:
    for sql in _ANALYSIS_TABLES_SQL:
        conn.execute(sql)


# --- shared in-process bus ---------------------------------------------

_BUS_LOCK = threading.Lock()
_SHARED_BUS: EventBus | None = None


def _get_shared_bus() -> EventBus:
    """Module-scoped :class:`EventBus` used for ``analyze`` fanout.

    Instantiated lazily so importing this module never spawns a worker
    thread on its own. Tests that need to observe events should inject
    their own bus via :func:`set_event_bus`.
    """
    global _SHARED_BUS
    with _BUS_LOCK:
        if _SHARED_BUS is None:
            _SHARED_BUS = EventBus()
        return _SHARED_BUS


def set_event_bus(bus: EventBus | None) -> None:
    """Override the module-scoped bus (primarily for tests)."""
    global _SHARED_BUS
    with _BUS_LOCK:
        _SHARED_BUS = bus


# --- connection -------------------------------------------------------

def open_conn(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the state DB read-write, applying Phase 5 and analysis schemas.

    Routes through :func:`apps.shared.state.db.open_rw` so the Phase 5
    PRAGMAs (WAL, foreign_keys, busy_timeout) and migrations run first,
    then the analysis-domain DDL runs on top. Safe to call repeatedly.
    """
    conn = state_db.open_rw(Path(db_path) if db_path is not None else None)
    _ensure_analysis_tables(conn)
    return conn


# --- upsert -----------------------------------------------------------

@dataclass
class UpsertResult:
    inserted: bool
    unchanged: bool


def _record_to_json(record: Any) -> str:
    maybe = getattr(record, "to_json", None)
    if callable(maybe):
        return maybe()
    data = asdict(record)
    data["analyzed_at"] = _dt_iso(data["analyzed_at"])
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


_IDEMPOTENCY_VOLATILE_KEYS: tuple[str, ...] = ("analyzed_at",)


def _semantic_equal(old_json: str, new_json: str) -> bool:
    try:
        old = json.loads(old_json)
        new = json.loads(new_json)
    except json.JSONDecodeError:
        return old_json == new_json
    for k in _IDEMPOTENCY_VOLATILE_KEYS:
        old.pop(k, None)
        new.pop(k, None)
    return old == new


def _dt_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def upsert(
    record: AnalysisRecord,
    db_path: Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> UpsertResult:
    """Insert/replace ``record``; idempotent on its semantic contents.

    If ``conn`` is provided the caller owns its lifecycle (used by batch
    drivers to avoid thousands of open/close cycles across a run). When
    ``conn`` is ``None`` a fresh connection is opened and closed per
    call; this preserves the original single-shot API for ad-hoc CLI
    use.
    """
    owned = conn is None
    if conn is None:
        conn = open_conn(db_path)
    try:
        row = conn.execute(
            """
            SELECT record_json FROM analysis
            WHERE stable_id = ? AND backend = ? AND backend_version = ?
            """,
            (record.stable_id, record.backend, record.backend_version),
        ).fetchone()
        new_json = _record_to_json(record)
        if row is not None and _semantic_equal(row[0], new_json):
            return UpsertResult(inserted=False, unchanged=True)

        conn.execute(
            """
            INSERT OR REPLACE INTO analysis (
                stable_id, backend, backend_version, analyzed_at,
                duration_s, sample_rate, bpm, bpm_confidence,
                key_camelot, key_openkey, key_confidence,
                energy, energy_source, record_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.stable_id,
                record.backend,
                record.backend_version,
                _dt_iso(record.analyzed_at),
                float(record.duration_s),
                int(record.sample_rate),
                float(record.bpm),
                float(record.bpm_confidence),
                record.key_camelot,
                record.key_openkey,
                float(record.key_confidence),
                int(record.energy),
                record.energy_source,
                new_json,
            ),
        )
        # open_rw returns an autocommit connection (isolation_level=None,
        # the Phase 5 shared-connection contract), so the INSERT above
        # has already been persisted. No explicit commit needed.
        return UpsertResult(inserted=row is None, unchanged=False)
    finally:
        if owned:
            conn.close()


def publish(
    event_type: str,
    payload: dict[str, Any],
    *,
    stable_id: str | None = None,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Append an event row (analysis_events + Phase 5 events) and fan out.

    ``conn`` is optional; when provided the caller owns its lifecycle
    (used by batch drivers to share one connection across many events).
    """
    ts = _now_iso()
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    owned = conn is None
    if conn is None:
        conn = open_conn(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO analysis_events (ts, event_type, stable_id, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (ts, event_type, stable_id, payload_json),
        )
        new_id = int(cur.lastrowid or 0)
        # Mirror into Phase 5's durable events table using its canonical
        # column names (kind, payload_json). Keeps the two logs in sync so
        # subscribers on the shared EventBus and archaeologists grepping
        # the events table both see every analyze write.
        conn.execute(
            """
            INSERT INTO events (ts, kind, stable_id, payload_json, actor)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ts, event_type, stable_id, payload_json, "apps.analysis"),
        )
    finally:
        if owned:
            conn.close()

    _get_shared_bus().publish(
        Event(
            ts=ts,
            kind=event_type,
            stable_id=stable_id,
            payload=payload,
            actor="apps.analysis",
        )
    )
    return new_id


def fetch_records(
    *,
    stable_ids: Iterable[str] | None = None,
    backend: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read analysis rows as dicts."""
    conn = open_conn(db_path)
    try:
        sql = "SELECT stable_id, backend, backend_version, record_json FROM analysis"
        params: list[Any] = []
        where: list[str] = []
        if stable_ids is not None:
            ids = list(stable_ids)
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            where.append(f"stable_id IN ({placeholders})")
            params.extend(ids)
        if backend is not None:
            where.append("backend = ?")
            params.append(backend)
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = conn.execute(sql, params).fetchall()
        return [
            {"stable_id": r[0], "backend": r[1], "backend_version": r[2], "record_json": r[3]}
            for r in rows
        ]
    finally:
        conn.close()


# --- high-level API (stable signatures for Phase 6 callers) ----------

def upsert_record(
    record: AnalysisRecord,
    *,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> UpsertResult:
    """Persist ``record``; emit an ``analyze`` event on insert/update.

    When ``conn`` is provided it is reused for both the upsert and the
    follow-up event publish, avoiding two open/close cycles per record.
    """
    result = upsert(record, db_path=db_path, conn=conn)
    if not result.unchanged:
        publish(
            event_type="analyze",
            payload={
                "stable_id": record.stable_id,
                "backend": record.backend,
                "backend_version": record.backend_version,
                "analyzed_at": _dt_iso(record.analyzed_at),
                "bpm": record.bpm,
                "key_camelot": record.key_camelot,
                "energy": record.energy,
                "inserted": result.inserted,
            },
            stable_id=record.stable_id,
            db_path=db_path,
            conn=conn,
        )
    return result


def publish_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    stable_id: str | None = None,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Generic event passthrough (used by 06-02 consumers)."""
    return publish(
        event_type=event_type,
        payload=payload,
        stable_id=stable_id,
        db_path=db_path,
        conn=conn,
    )


def fetch_records_by_ids(
    stable_ids: list[str],
    *,
    backend: str | None = None,
    db_path: Path | None = None,
) -> list[AnalysisRecord]:
    """Reconstruct :class:`AnalysisRecord` objects."""
    rows = fetch_records(stable_ids=stable_ids, backend=backend, db_path=db_path)
    return [AnalysisRecord.from_json(r["record_json"]) for r in rows]


__all__ = [
    "UpsertResult",
    "fetch_records",
    "fetch_records_by_ids",
    "open_conn",
    "publish",
    "publish_event",
    "set_event_bus",
    "upsert",
    "upsert_record",
]
