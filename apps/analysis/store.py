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

import itertools
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from apps.shared.events import publish as publish_engine_event
from apps.shared.lazy_event_bus import LazyModuleEventBus
from apps.shared.state import db as state_db
from apps.shared.state.analysis_stale import STALE_AUDIO_REPLACED, WHOLE_RECORD
from apps.shared.state.events import EventBus
from apps.shared.state.types import Event

from . import queue_store
from .analysis_records_tables_sql import ANALYSIS_RECORDS_TABLES_SQL
from .bpm_status import bpm_status_for
from .canonical import canonical_pointer, refresh_for_record
from .lane_enums import LANE_STATUSES
from .lane_payloads_shared import bar_phase_untrusted
from .native_tables_sql import NATIVE_ANALYSIS_V1_SQL
from .record import AnalysisRecord, StoredRecordError, validate_record_contract
from .time_iso import datetime_to_utc_iso, utc_now_iso

# --- additive schema ----------------------------------------------------

_ANALYSIS_TABLES_SQL: list[str] = [
    *ANALYSIS_RECORDS_TABLES_SQL,
    *NATIVE_ANALYSIS_V1_SQL,
]


# The backfill queue's own tables (native-analysis v1, spec section 3
# "Queue"): declared HERE with the rest of the analysis domain, for the same
# reason analysis_source_default is. The queue is durable state that the
# canonical pointer reads (analysis_stale is consulted by
# apps/analysis/canonical.py), so a schema authority that did not know about
# it would leave the pointer's inputs invisible to adoption, to the drift
# checks and to the database documentation.
_ANALYSIS_TABLES_SQL.extend(queue_store.QUEUE_TABLES_SQL)
_ANALYSIS_TABLES_SQL.extend(queue_store.STALE_TABLES_SQL)


def _ensure_analysis_tables(conn: sqlite3.Connection) -> None:
    for sql in _ANALYSIS_TABLES_SQL:
        conn.execute(sql)


# --- shared in-process bus ---------------------------------------------

_MODULE_BUS = LazyModuleEventBus()


def _get_shared_bus() -> EventBus:
    """Module-scoped :class:`EventBus` used for ``analyze`` fanout.

    Instantiated lazily so importing this module never spawns a worker
    thread on its own. Tests that need to observe events should inject
    their own bus via :func:`set_event_bus`.
    """
    return _MODULE_BUS.get()


def set_event_bus(bus: EventBus | None) -> None:
    """Override the module-scoped bus (primarily for tests)."""
    _MODULE_BUS.set(bus)


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
    # What the row now holds, which is not always what the caller passed: a
    # refused untrusted grid is replaced by the stored trusted one. Post-write
    # effects (the analyze event, the dependency cascade) must read THIS, or a
    # cascade hashes the rejected grid and drops a still-valid key.
    record: AnalysisRecord


def _record_to_json(record: Any) -> str:
    maybe = getattr(record, "to_json", None)
    if callable(maybe):
        return maybe()
    data = asdict(record)
    data["analyzed_at"] = datetime_to_utc_iso(data["analyzed_at"])
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


_IDEMPOTENCY_VOLATILE_KEYS: tuple[str, ...] = ("analyzed_at",)


def _json_record_object(raw: str, label: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StoredRecordError(f"{label} is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise StoredRecordError(f"{label} is a {type(data).__name__}, not an object")
    return data


def _lanes_object_from_record(data: dict[str, Any], label: str) -> dict[str, Any] | None:
    if "lanes" not in data:
        return None
    lanes = data["lanes"]
    if lanes is None:
        raise StoredRecordError(f"{label} has explicit null lanes")
    if not isinstance(lanes, dict):
        raise StoredRecordError(f"{label} has lanes of type {type(lanes).__name__}, not an object")
    return lanes


def _ok_beatgrid_payload_from_lanes(lanes: dict[str, Any], label: str) -> dict[str, Any] | None:
    if "beatgrid" not in lanes:
        return None
    lane = lanes["beatgrid"]
    if not isinstance(lane, dict):
        raise StoredRecordError(f"{label} has a beatgrid lane of type {type(lane).__name__}")
    status = lane.get("status")
    if not isinstance(status, str) or status not in LANE_STATUSES:
        raise StoredRecordError(f"{label} has beatgrid status {status!r}")
    if status != "ok":
        return None
    payload = lane.get("payload")
    if not isinstance(payload, dict):
        raise StoredRecordError(
            f"{label} has an ok beatgrid lane with a payload of type {type(payload).__name__}"
        )
    if "beats" not in payload:
        raise StoredRecordError(f"{label} ok beatgrid has no beats")
    beats = payload["beats"]
    if not isinstance(beats, list):
        raise StoredRecordError(f"{label} has beatgrid beats of type {type(beats).__name__}")
    bar_phase_untrusted(payload)
    return payload


def _beatgrid_payload(raw: str, label: str) -> dict[str, Any] | None:
    """The ``ok`` beatgrid payload in one record JSON, or None when there is none.

    None means absent and nothing else: no ``lanes`` (a row from before
    lanes), no ``beatgrid`` lane, or a lane whose status is not ``ok``. Any
    wrong shape raises. Reading it as "no trusted grid" would let the upsert
    that follows overwrite the corrupt row and hide the drift. The queue
    runner fails that one track on this error and carries on with the batch.
    """
    data = _json_record_object(raw, label)
    lanes = _lanes_object_from_record(data, label)
    if lanes is None:
        return None
    return _ok_beatgrid_payload_from_lanes(lanes, label)


def _untrusted_bar_phase_would_replace_trusted(
    old_json: str, new_json: str, stable_id: str
) -> bool:
    """True when a later beatgrid would replace a trusted one with an untrusted bar phase.

    Absent ``bar_phase_untrusted`` is trusted: grids written before the flag
    exist, and a vote above the floor omits it. An untrusted result may still
    replace an untrusted one, and a trusted result may replace an untrusted one.
    """
    old = _beatgrid_payload(old_json, f"stored analysis record for {stable_id!r}")
    new = _beatgrid_payload(new_json, f"new analysis record for {stable_id!r}")
    if old is None or new is None or not old.get("beats"):
        return False
    if bar_phase_untrusted(old):
        return False
    return bar_phase_untrusted(new)


def _served_beatgrid_json(
    conn: sqlite3.Connection, stable_id: str, retired: set[tuple[str, str]]
) -> str | None:
    """The record JSON the canonical beatgrid pointer names, or None when nothing (current) is served."""
    pointer = canonical_pointer(conn, stable_id, "beatgrid")
    if pointer is None or pointer in retired:
        return None
    served = conn.execute(
        "SELECT record_json FROM analysis WHERE stable_id = ? AND backend = ? AND backend_version = ?",
        (stable_id, pointer[0], pointer[1]),
    ).fetchone()
    return None if served is None else served[0]


def _with_stored_grid(record: AnalysisRecord, stored: AnalysisRecord) -> AnalysisRecord:
    """``record`` with the stored trusted beatgrid lane and its tempo put back."""
    return replace(
        record,
        bpm=stored.bpm,
        bpm_confidence=stored.bpm_confidence,
        lanes={**record.lanes, "beatgrid": stored.lanes["beatgrid"]},
    )


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


_SAVEPOINT_SEQ = itertools.count()


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a block as one all-or-nothing unit, nestable inside a caller's txn.

    Named uniquely per entry so a nested use cannot release an outer
    savepoint of the same name, which SQLite would accept and which would
    silently widen the unit being committed.

    Idle connections go through :func:`state_db.write_unit` (``BEGIN
    IMMEDIATE``) so a peer commit between the unit's first read and first
    write waits inside ``busy_timeout`` instead of raising SQLITE_BUSY_SNAPSHOT
    (STATE-15). Nested units stay SAVEPOINTs.
    """
    name = f"analysis_upsert_{next(_SAVEPOINT_SEQ)}"
    with state_db.write_unit(conn, name):
        yield


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
    # The v1 record contract is enforced HERE, at the write boundary, not in
    # the dataclass: a producer must be free to build a record incrementally,
    # but a record that reaches storage without a producer version, without a
    # decode fingerprint, or with a fabricated model hash is not writable
    # (NATIVE-09). Pre-v1 backends are exempt by name; see
    # validate_record_contract.
    validate_record_contract(record)
    owned = conn is None
    if conn is None:
        conn = open_conn(db_path)
    try:
        new_json = _record_to_json(record)
        _beatgrid_payload(new_json, f"new analysis record for {record.stable_id!r}")
        # Rows of bytes the file no longer holds (folder ingest's whole-record
        # marker): their grid describes other audio, so it is never kept.
        retired = queue_store.stale_rows(conn, record.stable_id, WHOLE_RECORD)
        # ONE atomic unit: the lookup, the record row, the canonical pointer,
        # and the whole projection rebuild. The SELECT used to run before the
        # unit, which pinned a DEFERRED WAL snapshot; a peer commit then made
        # the first write SQLITE_BUSY_SNAPSHOT (STATE-15 / OPEN-DJ-BE-7A).
        # write_unit takes the writer lock first on an idle connection.
        # Nested callers still get a SAVEPOINT.
        with _atomic(conn):
            row = conn.execute(
                """
                SELECT record_json FROM analysis
                WHERE stable_id = ? AND backend = ? AND backend_version = ?
                """,
                (record.stable_id, record.backend, record.backend_version),
            ).fetchone()
            # The trusted grid this write could displace: the same row's when it
            # holds one, else the one the canonical pointer serves now. A
            # producer-version bump (1.6.0 -> 1.7.0) writes a NEW row, and a
            # same-version row whose grid failed or is absent is not served, so
            # checking only that row would let an untrusted 1.7.0 grid outrank a
            # trusted 1.6.0 one on the pointer.
            trusted_json = next(
                (
                    candidate
                    for candidate in (
                        row[0]
                        if row is not None
                        and (record.backend, record.backend_version) not in retired
                        else None,
                        _served_beatgrid_json(conn, record.stable_id, retired),
                    )
                    if candidate is not None
                    and _untrusted_bar_phase_would_replace_trusted(
                        candidate, new_json, record.stable_id
                    )
                ),
                None,
            )
            if trusted_json is not None:
                # Only the grid (and the tempo it carries) is refused; every other
                # lane and scalar in this record still lands.
                record = _with_stored_grid(record, AnalysisRecord.from_json(trusted_json))
                new_json = _record_to_json(record)
            unchanged = row is not None and _semantic_equal(row[0], new_json)
            if not unchanged:
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
                        datetime_to_utc_iso(record.analyzed_at),
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
            # This write is its producer's claim about the current bytes, so the
            # audio-replaced markers for the whole row and for every lane it
            # supplies no longer withhold it. A lane it does not supply stays stale.
            for lane in (WHOLE_RECORD, *record.lanes):
                conn.execute(
                    "DELETE FROM analysis_stale WHERE stable_id = ? AND lane = ? AND backend = ?"
                    " AND backend_version = ? AND reason = ?",
                    (record.stable_id, lane, record.backend, record.backend_version, STALE_AUDIO_REPLACED),
                )
            # Refresh even when unchanged: an unchanged row can become
            # canonical once a HIGHER-versioned sibling is deleted, and a
            # pointer that only moved on a content change would go stale in
            # silence.
            refresh_for_record(conn, record)

        if unchanged:
            return UpsertResult(inserted=False, unchanged=True, record=record)
        return UpsertResult(inserted=row is None, unchanged=False, record=record)
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
    ts = utc_now_iso()
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
    cascade: bool = True,
    version_bump: bool = True,
    publish_live_event: bool = True,
) -> UpsertResult:
    """Persist ``record``; emit an ``analyze`` event on insert/update.

    When ``conn`` is provided it is reused for both the upsert and the
    follow-up event publish, avoiding two open/close cycles per record.

    ``cascade`` and ``version_bump`` run the NATIVE-10 side effects every
    non-unchanged write path shares. The queue runner passes ``False`` for
    both inside its item commit transaction and applies cascade itself so
    outcomes stay visible on ``RunSummary``.
    """
    result = upsert(record, db_path=db_path, conn=conn)
    # The persisted record, not the argument: see UpsertResult.record.
    record = result.record
    if not result.unchanged:
        # The ``bpm`` column keeps its 0.0 storage sentinel for a track with no
        # tempo, so the event names the status and reason beside it: a
        # subscriber or audit reader must never read the sentinel as a
        # measured tempo (issue #5986).
        bpm_status, bpm_reason = bpm_status_for(
            record.bpm, record.duration_s, record.features_blob
        )
        publish(
            event_type="analyze",
            payload={
                "stable_id": record.stable_id,
                "backend": record.backend,
                "backend_version": record.backend_version,
                "analyzed_at": datetime_to_utc_iso(record.analyzed_at),
                "bpm": record.bpm,
                "bpm_status": bpm_status,
                "bpm_reason": bpm_reason,
                "key_camelot": record.key_camelot,
                "energy": record.energy,
                "inserted": result.inserted,
            },
            stable_id=record.stable_id,
            db_path=db_path,
            conn=conn,
        )
        # The websocket invalidation bus is the live signal for loaded decks.
        # Keep the lane explicit so consumers can refresh only the affected
        # track and preserve the terminal missing-answer polling rule.
        from .lanes import parse_own_backend

        parsed_backend = parse_own_backend(record.backend)
        if publish_live_event and parsed_backend is not None:
            publish_engine_event(
                "analysis.changed",
                {"stable_id": record.stable_id, "lane": parsed_backend.lane},
            )
        if cascade or version_bump:
            from .queue_effects import apply_record_write_effects

            owned_effects = conn is None
            effect_conn = conn if conn is not None else open_conn(db_path)
            try:
                apply_record_write_effects(
                    effect_conn,
                    record,
                    result,
                    cascade=cascade,
                    version_bump=version_bump,
                )
            finally:
                if owned_effects:
                    effect_conn.close()
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
