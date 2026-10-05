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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import queue_stale
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
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


#-----------------------------------------------------------------------------
# pointer
#-----------------------------------------------------------------------------

def _canonical_beatgrid_record(
    conn: sqlite3.Connection, stable_id: str,
) -> AnalysisRecord | None:
    """The canonical own beatgrid record visible on ``conn`` (same transaction)."""
    pointer = canonical_pointer(conn, stable_id, "beatgrid")
    if pointer is None:
        return None
    row = conn.execute(
        "SELECT record_json FROM analysis "
        "WHERE stable_id = ? AND backend = ? AND backend_version = ?",
        (stable_id, pointer[0], pointer[1]),
    ).fetchone()
    if row is None:
        return None
    record = AnalysisRecord.from_json(row[0])
    beatgrid_lane = record.lanes.get("beatgrid")
    if beatgrid_lane is None or beatgrid_lane.status != "ok":
        return None
    return record


def _key_row_is_stale(
    conn: sqlite3.Connection, stable_id: str, record_json: str,
) -> bool:
    """True when an ok key record's beatgrid dependency no longer matches."""
    from .depends_on import dependency_identity, dependency_matches

    record = AnalysisRecord.from_json(record_json)
    key_lane = record.lanes.get("key")
    if key_lane is None or key_lane.status != "ok":
        return False
    depends_on = key_lane.payload.get("depends_on", {}).get("beatgrid")
    if not isinstance(depends_on, dict):
        return False
    current = _canonical_beatgrid_record(conn, stable_id)
    if current is None:
        # No canonical beatgrid to compare against yet: the row may still win
        # canonical for scalar projection. Segment staleness is enforced at
        # read time in the /anlz overlay.
        return False
    return not dependency_matches(depends_on, dependency_identity(current))


def key_lane_stale_but_unpromoted(
    conn: sqlite3.Connection, stable_id: str,
) -> bool:
    """True when ok own key rows exist but canonical is empty due to stale beatgrid."""
    if canonical_pointer(conn, stable_id, "key") is not None:
        return False
    rows = conn.execute(
        "SELECT record_json FROM analysis WHERE stable_id = ?",
        (stable_id,),
    ).fetchall()
    for (record_json,) in rows:
        record = AnalysisRecord.from_json(record_json)
        parsed = parse_own_backend(record.backend)
        if parsed is None or parsed.lane != "key" or parsed.producer == "cand":
            continue
        key_lane = record.lanes.get("key")
        if key_lane is None or key_lane.status != "ok":
            continue
        if _key_row_is_stale(conn, stable_id, record_json):
            return True
    return False


def _eligible_rows(
    conn: sqlite3.Connection, stable_id: str, lane: str,
) -> list[tuple[str, str, str]]:
    """(backend, backend_version, record_json) for rows that may be canonical.

    Three exclusions, in this order: a row that is not an own record for
    this lane, a bench candidate, and a row the queue has marked STALE.

    The third is the dependency cascade (native-analysis v1, spec section 3;
    :mod:`apps.analysis.queue`). A key record computed against beatgrid v1
    is not wrong in itself, but once the canonical beatgrid moves it no
    longer describes the grid the deck reads, so it must stop being the
    canonical key until it is recomputed. Excluding it here rather than
    deleting the row keeps the record (rows never overwrite across
    producers) while taking it out of the pointer, which is exactly what the
    spec asks for.
    """
    rows = conn.execute(
        "SELECT backend, backend_version, record_json FROM analysis WHERE stable_id = ?",
        (stable_id,),
    ).fetchall()
    stale = queue_stale.stale_rows(conn, stable_id, lane)
    out: list[tuple[str, str, str]] = []
    for backend, backend_version, record_json in rows:
        parsed = parse_own_backend(backend)
        if parsed is None or parsed.lane != lane or parsed.producer == "cand":
            continue
        if lane == "key" and _key_row_is_stale(conn, stable_id, record_json):
            continue
        if (backend, backend_version) in stale:
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
    if canonical_pointer(conn, stable_id, lane) == (winner[0], winner[1]):
        # Identical pointer: leave the row, and its timestamp, alone. That
        # stamp is folded into the track's public updated_at and therefore
        # its ETag, so rewriting it on a semantically unchanged re-run would
        # invalidate every client cache for a track nothing changed about.
        return (winner[0], winner[1])
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


def canonical_pointed_ids(
    conn: sqlite3.Connection, stable_ids: Sequence[str], lane: str,
) -> set[str]:
    """The subset of ``stable_ids`` that has a stored pointer for ``lane``.

    The batched twin of :func:`canonical_pointer` for callers that need only
    "is there one": one statement per 500 ids instead of one per track.
    """
    pointed: set[str] = set()
    for start in range(0, len(stable_ids), 500):
        chunk = stable_ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        pointed.update(
            str(row[0]) for row in conn.execute(
                "SELECT stable_id FROM analysis_canonical "
                f"WHERE lane = ? AND stable_id IN ({placeholders})",
                (lane, *chunk),
            )
        )
    return pointed


#-----------------------------------------------------------------------------
# projection
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class _Projected:
    """One projected scalar, carrying the status of whatever produced it.

    ``status`` is per FIELD, not per lane, because a lane can partly
    succeed: the key lane's `segments` block has its own status (it needs
    own downbeats, which can be absent while the global key succeeded), so
    `key` can be `ok` while `key_change_count` is `failed` with the segment
    analysis's OWN reason. Collapsing that to a generic `missing` would
    make a measured failure indistinguishable from analysis that never ran.
    """

    value: Any
    confidence: float | None = None
    status: str = "ok"
    reason: str | None = None


def _project_lane(lane: str, result: LaneResult) -> dict[str, _Projected]:
    """Scalars a successful lane contributes."""
    payload = result.payload
    if lane == "beatgrid":
        return {
            "bpm": _Projected(float(payload["bpm"]), float(payload["bpm_confidence"])),
            "tempo_change_count": _Projected(
                len(payload["tempo_changes"]), result.confidence
            ),
        }
    if lane == "key":
        return {
            "key": _Projected(str(payload["camelot"]), float(payload["confidence"])),
            "key_change_count": _key_change_count(payload),
        }
    if lane == "loudness":
        return {
            "loudness_lufs": _Projected(
                float(payload["integrated_lufs"]), result.confidence
            ),
            "loudness_dbtp": _Projected(
                float(payload["true_peak_dbtp"]), result.confidence
            ),
        }
    # waveform and vocal contribute no scalars; they are served through /anlz.
    return {}


def _key_change_count(payload: dict[str, Any]) -> _Projected:
    """A stable key is ONE segment, so the count of CHANGES is one fewer.

    The segments block's own status and reason travel through verbatim.
    """
    segments_block = payload["segments"]
    if segments_block["status"] != "ok":
        return _Projected(
            value=None, confidence=None,
            status=segments_block["status"],
            reason=segments_block.get("reason") or "key_segments_unavailable",
        )
    return _Projected(max(len(segments_block["segments"]) - 1, 0))


@dataclass(frozen=True)
class _Pointer:
    """The canonical row a projection row was derived from."""

    backend: str
    backend_version: str


def _write_projection_row(
    conn: sqlite3.Connection,
    stable_id: str,
    field: str,
    *,
    value: Any,
    status: str,
    reason: str | None,
    confidence: float | None,
    pointer: _Pointer,
) -> None:
    existing = conn.execute(
        "SELECT value, status, reason, confidence, backend, backend_version "
        "FROM analysis_projection WHERE stable_id = ? AND field = ?",
        (stable_id, field),
    ).fetchone()
    if existing is not None and tuple(existing) == (
        value, status, reason, confidence, pointer.backend, pointer.backend_version
    ):
        # Same in every column that carries meaning, so the row is not
        # rewritten and `updated_at` does not move. See recompute_canonical:
        # this stamp reaches the track ETag.
        return
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
            pointer.backend, pointer.backend_version, _now_iso(),
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
    ptr = _Pointer(backend=backend, backend_version=backend_version)
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
            cell = projected[field_name]
            _write_projection_row(
                conn, stable_id, field_name,
                value=cell.value, status=cell.status, reason=cell.reason,
                confidence=cell.confidence, pointer=ptr,
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
            confidence=None, pointer=ptr,
        )


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
    lanes = set(record.lanes) | {parsed.lane}
    if parsed.lane == "beatgrid":
        # An own beatgrid write can stale every ok key record that names it.
        # Recompute key canonical + projection in the same transaction so
        # effective_fields() does not keep serving a scalar from a dependency
        # the /anlz overlay already treats as stale_dependency.
        lanes.add("key")
    refresh_lanes(conn, record.stable_id, sorted(lanes))


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
    "canonical_pointed_ids",
    "canonical_pointer",
    "rebuild_projection",
    "recompute_canonical",
    "refresh_for_record",
    "refresh_lanes",
]
