"""Phase 6 state-layer shim for ``analysis`` rows + events.

DELETE-ON-SIGHT once Phase 5 ships ``apps.shared.state.analysis.upsert`` +
a canonical ``analyze`` event via its ``StateWriter`` / ``EventBus`` pair.

Design:

* Adds two additive tables (``analysis`` + ``analysis_events``) to the same
  SQLite file Phase 5 uses (``STATE_DB``).  The migration is independent of
  Phase 5's ``schema_meta`` counter so they never clash.
* Idempotent on ``(stable_id, backend, backend_version)``.
* ``analyzed_at`` is excluded from the idempotency comparison so re-running
  the same backend on the same inputs is a true no-op.

See 06-01-PLAN §Step 5.
"""
# TODO(delete-when-phase-05-lands): see module docstring.
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from apps.shared.paths import STATE_DB

_SHIM_TABLES_SQL: list[str] = [
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


def _ensure_parent(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)


def open_conn(db_path: Path | None = None) -> sqlite3.Connection:
    """Open (and migrate) the state DB."""
    path = Path(db_path) if db_path else STATE_DB
    _ensure_parent(path)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for sql in _SHIM_TABLES_SQL:
        conn.execute(sql)
    conn.commit()


@dataclass
class UpsertResult:
    inserted: bool
    unchanged: bool


def upsert(record: "RecordLike", db_path: Path | None = None) -> UpsertResult:
    """Insert/replace ``record``; idempotent semantically on its contents."""
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
        conn.commit()
        return UpsertResult(inserted=row is None, unchanged=False)
    finally:
        conn.close()


def publish(
    event_type: str,
    payload: dict[str, Any],
    *,
    stable_id: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Append an event row; returns the new id."""
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO analysis_events (ts, event_type, stable_id, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                event_type,
                stable_id,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def fetch_records(
    *,
    stable_ids: Iterable[str] | None = None,
    backend: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read rows as dicts.  Callers can pass ``record_json`` to AnalysisRecord.from_json."""
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


# --- helpers ------------------------------------------------------------


class RecordLike:  # noqa: D401 - typing placeholder
    """Structural type helper (see module docstring)."""


def _dt_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _record_to_json(record: "RecordLike") -> str:
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


__all__ = [
    "open_conn",
    "upsert",
    "publish",
    "fetch_records",
    "UpsertResult",
]
