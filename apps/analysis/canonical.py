"""Canonical pointer + projection rebuild for own analysis records.

`specs/native-analysis-v1.md` section 3, "Record":

    Rows never overwrite across producers, so run order cannot change what
    exists. Canonical selection is a separate, deterministic pointer:
    ``analysis_canonical(stable_id, lane) -> (backend, backend_version)``
    recomputed on every write by one rule, highest ``backend_version``
    wins, and at equal version ``inapp`` beats ``backfill``. Bench
    candidate rows (``cand.*``) are never eligible.

The pointer is recomputed from EVERY own row for that (track, lane), not
patched from the row being written. That is what buys order independence:
"write backfill then inapp" and "write inapp then backfill" both end at
the same query over the same set. A patch-forward implementation would
have to reason about which row is newer and would be exactly as
order-dependent as the last-writer-wins column this replaces.

The projection is rebuilt from the canonical row, never from the row being
written, for the same reason. It holds the own scalars a track view or a
smartlist reads (``bpm``, ``key``, ``loudness_lufs``, ``loudness_dbtp``,
``key_change_count``, ``tempo_change_count``) and it is READ-ONLY state as
far as the library is concerned: nothing here writes ``track_fields``, so
no own value can enter ``track_field_history`` or the sync path.

-Claude
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from .lanes import LANES, LaneResult, SemverError, parse_own_backend, semver_key
from .record import AnalysisRecord

# Which lane owns each projected scalar. The lane is the SELECTION unit, so
# a field's effective source is its lane's source, never its own.
PROJECTION_FIELDS: dict[str, str] = {
    "bpm": "beatgrid",
    "tempo_change_count": "beatgrid",
    "key": "key",
    "key_change_count": "key",
    "loudness_lufs": "loudness",
    "loudness_dbtp": "loudness",
}

FIELDS_BY_LANE: dict[str, tuple[str, ...]] = {
    lane: tuple(f for f, ln in PROJECTION_FIELDS.items() if ln == lane)
    for lane in LANES
}

# inapp beats backfill at equal version: the deck ships the in-app producer,
# and backfill is the calibration path. cand is absent on purpose -- a bench
# candidate is never eligible, so it is filtered out before ranking rather
# than ranked last, where one bad comparison could still promote it.
_PRODUCER_RANK: dict[str, int] = {"backfill": 0, "inapp": 1}


class CanonicalError(ValueError):
    """The canonical pointer cannot be computed for a (track, lane)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


#-----------------------------------------------------------------------------
# pointer
#-----------------------------------------------------------------------------

def _eligible_rows(
    conn: sqlite3.Connection, stable_id: str, lane: str,
) -> list[tuple[str, str, str]]:
    """(backend, backend_version, record_json) for rows that may be canonical."""
    rows = conn.execute(
        "SELECT backend, backend_version, record_json FROM analysis WHERE stable_id = ?",
        (stable_id,),
    ).fetchall()
    out: list[tuple[str, str, str]] = []
    for backend, backend_version, record_json in rows:
        parsed = parse_own_backend(backend)
        if parsed is None or parsed.lane != lane or parsed.producer == "cand":
            continue
        out.append((backend, backend_version, record_json))
    return out


def recompute_canonical(
    conn: sqlite3.Connection, stable_id: str, lane: str,
) -> tuple[str, str] | None:
    """Recompute ``analysis_canonical`` for one (track, lane) from all rows.

    Returns the winning ``(backend, backend_version)`` or ``None`` when no
    eligible own row exists, in which case the pointer row is removed.
    """
    if lane not in LANES:
        raise CanonicalError(f"unknown lane {lane!r}; lanes are {LANES}")
    candidates = _eligible_rows(conn, stable_id, lane)
    if not candidates:
        conn.execute(
            "DELETE FROM analysis_canonical WHERE stable_id = ? AND lane = ?",
            (stable_id, lane),
        )
        return None

    def rank(row: tuple[str, str, str]) -> tuple[Any, int]:
        backend, backend_version, _ = row
        parsed = parse_own_backend(backend)
        assert parsed is not None  # filtered above
        try:
            version_key = semver_key(backend_version)
        except SemverError as exc:
            raise CanonicalError(
                f"row {backend!r} for {stable_id!r} cannot be ranked: {exc}"
            ) from exc
        return (version_key, _PRODUCER_RANK[parsed.producer])

    winner = max(candidates, key=rank)
    conn.execute(
        """
        INSERT INTO analysis_canonical (stable_id, lane, backend, backend_version, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(stable_id, lane) DO UPDATE SET
            backend = excluded.backend,
            backend_version = excluded.backend_version,
            updated_at = excluded.updated_at
        """,
        (stable_id, lane, winner[0], winner[1], _now_iso()),
    )
    return (winner[0], winner[1])


def canonical_pointer(
    conn: sqlite3.Connection, stable_id: str, lane: str,
) -> tuple[str, str] | None:
    """Read the stored pointer without recomputing it."""
    row = conn.execute(
        "SELECT backend, backend_version FROM analysis_canonical "
        "WHERE stable_id = ? AND lane = ?",
        (stable_id, lane),
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


#-----------------------------------------------------------------------------
# projection
#-----------------------------------------------------------------------------

def _project_lane(lane: str, result: LaneResult) -> dict[str, tuple[Any, float | None]]:
    """Scalars a successful lane contributes, as ``field -> (value, confidence)``."""
    payload = result.payload
    if lane == "beatgrid":
        return {
            "bpm": (float(payload["bpm"]), float(payload["bpm_confidence"])),
            "tempo_change_count": (len(payload["tempo_changes"]), result.confidence),
        }
    if lane == "key":
        return {
            "key": (str(payload["camelot"]), float(payload["confidence"])),
            # A stable key is ONE segment, so the count of CHANGES is one
            # fewer. Reported only when the segment analysis itself is ok:
            # key-change analysis needs own downbeats and can be missing
            # while the global key succeeded, which is why the segments
            # block carries its own status (spec section 3).
            "key_change_count": _key_change_count(payload),
        }
    if lane == "loudness":
        return {
            "loudness_lufs": (float(payload["integrated_lufs"]), result.confidence),
            "loudness_dbtp": (float(payload["true_peak_dbtp"]), result.confidence),
        }
    # waveform and vocal contribute no scalars; they are served through /anlz.
    return {}


def _key_change_count(payload: dict[str, Any]) -> tuple[Any, float | None]:
    segments_block = payload["segments"]
    if segments_block["status"] != "ok":
        return (None, None)
    return (max(len(segments_block["segments"]) - 1, 0), None)


def _write_projection_row(
    conn: sqlite3.Connection,
    stable_id: str,
    field: str,
    *,
    value: Any,
    status: str,
    reason: str | None,
    confidence: float | None,
    backend: str,
    backend_version: str,
) -> None:
    conn.execute(
        """
        INSERT INTO analysis_projection (
            stable_id, field, value, status, reason, confidence,
            backend, backend_version, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stable_id, field) DO UPDATE SET
            value = excluded.value,
            status = excluded.status,
            reason = excluded.reason,
            confidence = excluded.confidence,
            backend = excluded.backend,
            backend_version = excluded.backend_version,
            updated_at = excluded.updated_at
        """,
        (
            stable_id, field, value, status, reason, confidence,
            backend, backend_version, _now_iso(),
        ),
    )


def _delete_lane_projection(conn: sqlite3.Connection, stable_id: str, lane: str) -> None:
    fields = FIELDS_BY_LANE[lane]
    if not fields:
        return
    placeholders = ",".join("?" * len(fields))
    conn.execute(
        f"DELETE FROM analysis_projection WHERE stable_id = ? AND field IN ({placeholders})",
        (stable_id, *fields),
    )


def rebuild_projection(conn: sqlite3.Connection, stable_id: str, lane: str) -> None:
    """Rebuild ``analysis_projection`` for one (track, lane) from the pointer.

    Reads the CANONICAL row, never the row that triggered the rebuild, so
    the projection is a pure function of the pointer and inherits its order
    independence.
    """
    if lane not in LANES:
        raise CanonicalError(f"unknown lane {lane!r}; lanes are {LANES}")
    if not FIELDS_BY_LANE[lane]:
        return
    pointer = canonical_pointer(conn, stable_id, lane)
    if pointer is None:
        _delete_lane_projection(conn, stable_id, lane)
        return
    backend, backend_version = pointer
    row = conn.execute(
        "SELECT record_json FROM analysis "
        "WHERE stable_id = ? AND backend = ? AND backend_version = ?",
        (stable_id, backend, backend_version),
    ).fetchone()
    if row is None:
        raise CanonicalError(
            f"canonical pointer for {stable_id!r}/{lane} names {backend!r}@"
            f"{backend_version!r} but no such analysis row exists"
        )
    record = AnalysisRecord.from_json(row[0])
    result = record.lanes.get(lane)
    if result is None:
        raise CanonicalError(
            f"canonical record {backend!r} for {stable_id!r} carries no {lane!r} lane"
        )

    if result.status == "ok":
        projected = _project_lane(lane, result)
        missing_fields = [f for f in FIELDS_BY_LANE[lane] if f not in projected]
        if missing_fields:
            raise CanonicalError(
                f"{lane} lane projects {sorted(projected)} but the projection "
                f"contract requires {list(FIELDS_BY_LANE[lane])}; missing "
                f"{missing_fields}"
            )
        for field_name in FIELDS_BY_LANE[lane]:
            value, confidence = projected[field_name]
            status = "ok" if value is not None else "missing"
            _write_projection_row(
                conn, stable_id, field_name,
                value=value, status=status,
                reason=None if status == "ok" else _sub_lane_reason(lane, field_name),
                confidence=confidence,
                backend=backend, backend_version=backend_version,
            )
        return

    # failed / missing: the field exists in the read model and carries the
    # lane's named reason with a null value. That is what lets a track row
    # render "failed: no_tonal_center" instead of a blank cell that reads as
    # ordinary missing metadata (spec section 3, NATIVE-04).
    for field_name in FIELDS_BY_LANE[lane]:
        _write_projection_row(
            conn, stable_id, field_name,
            value=None, status=result.status, reason=result.reason,
            confidence=None, backend=backend, backend_version=backend_version,
        )


def _sub_lane_reason(lane: str, field_name: str) -> str:
    if field_name == "key_change_count":
        return "key_segments_unavailable"
    return f"{lane}_field_unavailable"


#-----------------------------------------------------------------------------
# entry point used by the store on every own upsert
#-----------------------------------------------------------------------------

def refresh_for_record(conn: sqlite3.Connection, record: AnalysisRecord) -> None:
    """Recompute pointer + projection for every lane the record touches.

    Called on every ``analysis`` upsert. A non-own record touches nothing:
    it can never be canonical, so no pointer it is absent from can change.
    """
    parsed = parse_own_backend(record.backend)
    if parsed is None:
        return
    refresh_lanes(conn, record.stable_id, sorted(set(record.lanes) | {parsed.lane}))


def refresh_lanes(
    conn: sqlite3.Connection, stable_id: str, lanes: Iterable[str],
) -> None:
    for lane in lanes:
        recompute_canonical(conn, stable_id, lane)
        rebuild_projection(conn, stable_id, lane)


__all__ = [
    "FIELDS_BY_LANE",
    "PROJECTION_FIELDS",
    "CanonicalError",
    "canonical_pointer",
    "rebuild_projection",
    "recompute_canonical",
    "refresh_for_record",
    "refresh_lanes",
]
