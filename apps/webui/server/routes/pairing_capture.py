"""Pairing-capture router -- LV1 sync snapshots + LV2 alignment marks (PAIR-02).

Validated HTTP surface over the durable :class:`PairingCaptureRepo` from
Part 1 (issue #1208). Append-only capture, so the contract is GET + POST
only -- no PUT/DELETE, matching the repository's own operations:

  * ``GET  /api/v1/pairings/sync-snapshots``  -- list, filterable by
    ``stable_a``/``stable_b``, capped by ``limit`` (default 50, max 200).
  * ``POST /api/v1/pairings/sync-snapshots``  -- capture one LV1 snapshot.
  * ``GET  /api/v1/pairings/alignments``      -- list, filterable by
    ``stable_a``/``stable_b`` (either side matches, like the repo).
  * ``POST /api/v1/pairings/alignments``      -- capture one LV2 mark.

Agent-native parity: every route above is plain HTTP, reachable identically
from the UI part 3 will build and from a bare ``curl``/``httpx`` call or the
CLI -- there is no UI-only path.

Error contract (explicit ``{"detail": {code, message}}`` like the rest of
the daemon):

  * 503 ``PAIRING_CAPTURE_DB_UNAVAILABLE`` -- no state.db on disk.
  * 422 ``PAIRING_CAPTURE_INVALID``        -- repository validation failed
    (same-track pairing, bad enum, non-finite/negative position or ratio).

Read routes never construct the repo with ``ensure_schema=True``: that runs
``CREATE TABLE IF NOT EXISTS`` under the hood, which a ``query_only``
connection rightly refuses (smartlists.py established this pattern).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.pairings.capture_repo import (
    Alignment,
    PairingCaptureError,
    PairingCaptureRepo,
    SyncSnapshot,
)

from ..backend import StateBackend
from ..deps import get_write_state

router = APIRouter(prefix="/pairings", tags=["pairings-capture"])


# ----------------------------------------------------------- schemas


class SyncSnapshotIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_a: str
    stable_b: str
    master_side: Literal["a", "b"]
    sync_mode: Literal["bar", "beat"]
    a_tempo_ratio: float = Field(ge=0)
    b_tempo_ratio: float = Field(ge=0)
    a_position_ms: float = Field(ge=0)
    b_position_ms: float = Field(ge=0)
    a_position_beat_n: int | None = None
    a_position_phase: float | None = None
    b_position_beat_n: int | None = None
    b_position_phase: float | None = None


class SyncSnapshotOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    stable_a: str
    stable_b: str
    master_side: Literal["a", "b"]
    sync_mode: Literal["bar", "beat"]
    a_tempo_ratio: float
    b_tempo_ratio: float
    a_position_beat_n: int | None
    a_position_phase: float | None
    a_position_ms: float
    b_position_beat_n: int | None
    b_position_phase: float | None
    b_position_ms: float
    captured_at: str


class AlignmentIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_a: str
    stable_b: str
    anchor_a_kind: Literal["hotcue", "ms"]
    anchor_b_kind: Literal["hotcue", "ms"]
    anchor_a_ms: float = Field(ge=0)
    anchor_b_ms: float = Field(ge=0)
    anchor_a_slot: str | None = None
    anchor_b_slot: str | None = None
    label: str | None = Field(default=None, max_length=200)


class AlignmentOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    stable_a: str
    stable_b: str
    anchor_a_kind: Literal["hotcue", "ms"]
    anchor_b_kind: Literal["hotcue", "ms"]
    anchor_a_slot: str | None
    anchor_b_slot: str | None
    anchor_a_ms: float
    anchor_b_ms: float
    label: str | None
    created_at: str


# ----------------------------------------------------------- _helpers


def _snapshot_out(row: SyncSnapshot) -> SyncSnapshotOut:
    return SyncSnapshotOut(
        id=row.id, stable_a=row.stable_a, stable_b=row.stable_b,
        master_side=row.master_side, sync_mode=row.sync_mode,
        a_tempo_ratio=row.a_tempo_ratio, b_tempo_ratio=row.b_tempo_ratio,
        a_position_beat_n=row.a_position_beat_n, a_position_phase=row.a_position_phase,
        a_position_ms=row.a_position_ms, b_position_beat_n=row.b_position_beat_n,
        b_position_phase=row.b_position_phase, b_position_ms=row.b_position_ms,
        captured_at=row.captured_at,
    )


def _alignment_out(row: Alignment) -> AlignmentOut:
    return AlignmentOut(
        id=row.id, stable_a=row.stable_a, stable_b=row.stable_b,
        anchor_a_kind=row.anchor_a_kind, anchor_b_kind=row.anchor_b_kind,
        anchor_a_slot=row.anchor_a_slot, anchor_b_slot=row.anchor_b_slot,
        anchor_a_ms=row.anchor_a_ms, anchor_b_ms=row.anchor_b_ms,
        label=row.label, created_at=row.created_at,
    )


def _db_path(request: Request) -> Path:
    return Path(getattr(request.app.state, "state_db_path", "data/state/state.db"))


def _unavailable(db_path: Path) -> HTTPException:
    return HTTPException(status_code=503, detail={
        "code": "PAIRING_CAPTURE_DB_UNAVAILABLE",
        "message": (
            f"state DB not found at {db_path}; run "
            "`python -m apps.shared.state.cli init` first."
        ),
    })


def get_capture_read_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Read-only sqlite conn on the daemon's state.db (query_only guard).

    Never runs migrations: a query_only connection rightly refuses the
    ``CREATE TABLE IF NOT EXISTS`` inside ``ensure_schema``.
    """
    db_path = _db_path(request)
    if not db_path.exists():
        raise _unavailable(db_path)
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True, isolation_level=None,
        check_same_thread=False,
    )
    conn.execute("PRAGMA query_only = ON")
    try:
        yield conn
    finally:
        conn.close()


def get_capture_write_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Writable, autocommit connection for capture inserts."""
    db_path = _db_path(request)
    if not db_path.exists():
        raise _unavailable(db_path)
    conn = sqlite3.connect(
        str(db_path), isolation_level=None, check_same_thread=False,
    )
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone()
    return row is not None


ReadConn = Annotated[sqlite3.Connection, Depends(get_capture_read_conn)]
WriteConn = Annotated[sqlite3.Connection, Depends(get_capture_write_conn)]
WriteGate = Annotated[StateBackend, Depends(get_write_state)]


# ----------------------------------------------------------- endpoints


@router.get("/sync-snapshots", response_model=list[SyncSnapshotOut])
def list_sync_snapshots(
    conn: ReadConn,
    stable_a: str | None = Query(None),
    stable_b: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> list[SyncSnapshotOut]:
    # Pre-Phase-08 db predating the capture migration has zero snapshots --
    # that is the honest state, not a failure to mask (smartlists.py
    # established the same reasoning for its own lazily-created table).
    if not _table_exists(conn, "pairing_sync_snapshots"):
        return []
    repo = PairingCaptureRepo(conn, ensure_schema=False)
    rows = repo.list_snapshots(stable_a=stable_a, stable_b=stable_b, limit=limit)
    return [_snapshot_out(r) for r in rows]


@router.post(
    "/sync-snapshots",
    response_model=SyncSnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def create_sync_snapshot(
    body: SyncSnapshotIn,
    conn: WriteConn,
    _backend: WriteGate,
) -> SyncSnapshotOut:
    repo = PairingCaptureRepo(conn)
    try:
        row = repo.add_snapshot(
            stable_a=body.stable_a, stable_b=body.stable_b,
            master_side=body.master_side, sync_mode=body.sync_mode,
            a_tempo_ratio=body.a_tempo_ratio, b_tempo_ratio=body.b_tempo_ratio,
            a_position_ms=body.a_position_ms, b_position_ms=body.b_position_ms,
            a_position_beat_n=body.a_position_beat_n,
            a_position_phase=body.a_position_phase,
            b_position_beat_n=body.b_position_beat_n,
            b_position_phase=body.b_position_phase,
        )
    except PairingCaptureError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "PAIRING_CAPTURE_INVALID", "message": str(exc),
        }) from exc
    return _snapshot_out(row)


@router.get("/alignments", response_model=list[AlignmentOut])
def list_alignments(
    conn: ReadConn,
    stable_a: str | None = Query(None),
    stable_b: str | None = Query(None),
) -> list[AlignmentOut]:
    if not _table_exists(conn, "pairing_alignments"):
        return []
    repo = PairingCaptureRepo(conn, ensure_schema=False)
    rows = repo.list_alignments(stable_a=stable_a, stable_b=stable_b)
    return [_alignment_out(r) for r in rows]


@router.post(
    "/alignments",
    response_model=AlignmentOut,
    status_code=status.HTTP_201_CREATED,
)
def create_alignment(
    body: AlignmentIn,
    conn: WriteConn,
    _backend: WriteGate,
) -> AlignmentOut:
    repo = PairingCaptureRepo(conn)
    try:
        row = repo.add_alignment(
            stable_a=body.stable_a, stable_b=body.stable_b,
            anchor_a_kind=body.anchor_a_kind, anchor_b_kind=body.anchor_b_kind,
            anchor_a_ms=body.anchor_a_ms, anchor_b_ms=body.anchor_b_ms,
            anchor_a_slot=body.anchor_a_slot, anchor_b_slot=body.anchor_b_slot,
            label=body.label,
        )
    except PairingCaptureError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "PAIRING_CAPTURE_INVALID", "message": str(exc),
        }) from exc
    return _alignment_out(row)


__all__ = [
    "AlignmentIn",
    "AlignmentOut",
    "SyncSnapshotIn",
    "SyncSnapshotOut",
    "get_capture_read_conn",
    "get_capture_write_conn",
    "router",
]
