"""The hub side of the sync protocol, as a FastAPI router.

Mounted by the webui at ``/api/v1/sync/*``:

    POST /api/v1/sync/hello    register a spoke, learn the hub id and seq
    POST /api/v1/sync/push     offer rows; the hub merges them under LWW
    GET  /api/v1/sync/pull     rows accepted after ``since_seq``
    GET  /api/v1/sync/status   hub identity, seq, fleet, row counts
    GET  /api/v1/sync/digest   per-table digests for the post-sync compare

Trust in v1 is tailnet membership (ADR 04 c7): any process that can reach the
daemon can push. That is deliberate for a personal fleet and must be revisited
before any multi-user deployment.

The hub DB is opened per request from ``app.state.state_db_path`` and closed
again, so the router holds no connection across requests and needs no lock of
its own. Every mutating handler runs in one explicit transaction: a push that
fails halfway leaves the hub exactly as it was.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.shared.state import schema as state_schema
from apps.sync_hub import engine, protocol

router = APIRouter(prefix="/sync", tags=["sync"])


# ----- request / response models ------------------------------------------


class MachineModel(BaseModel):
    """One ``machines`` row on the wire."""

    machine_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    is_hub: bool = False
    data_root: str | None = None
    first_seen: str = Field(min_length=1)
    last_seen: str = Field(min_length=1)


class RowModel(BaseModel):
    """One offered row. ``members`` is set only on a ``playlists`` row."""

    table: str = Field(min_length=1)
    pk: list[str] = Field(min_length=1)
    values: dict[str, Any]
    members: list[dict[str, Any]] | None = None


class HelloRequest(BaseModel):
    machine: MachineModel
    schema_version: int


class HelloResponse(BaseModel):
    hub_machine_id: str
    schema_version: int
    seq: int
    machines: list[MachineModel]


class PushRequest(BaseModel):
    machine_id: str = Field(min_length=1)
    schema_version: int
    rows: list[RowModel]


class PushResponse(BaseModel):
    accepted: int
    rejected: int
    seq: int


class PullResponse(BaseModel):
    rows: list[RowModel]
    seq: int
    machines: list[MachineModel]


class StatusResponse(BaseModel):
    hub_machine_id: str
    schema_version: int
    seq: int
    machines: list[MachineModel]
    row_counts: dict[str, int]


class DigestResponse(BaseModel):
    tables: dict[str, str]
    overall: str


# ----- wiring helpers ------------------------------------------------------


def _db_path(request: Request) -> Path:
    configured = getattr(request.app.state, "state_db_path", None)
    if configured is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SYNC_NO_DB",
                "message": (
                    "app.state.state_db_path is unset; the hub has no state DB "
                    "to sync against."
                ),
            },
        )
    return Path(str(configured))


def _data_dir(request: Request) -> Path:
    """Where this hub's ``machine-id`` file lives.

    Derived from the DB path (``<data-dir>/state/state.db``) unless the app
    sets ``sync_hub_data_dir`` explicitly. No env fallback: a hub that cannot
    say which data root it owns should fail, not adopt a plausible one.
    """
    configured = getattr(request.app.state, "sync_hub_data_dir", None)
    if configured is not None:
        return Path(str(configured))
    return _db_path(request).resolve().parent.parent


def _machine_name(request: Request) -> str | None:
    configured = getattr(request.app.state, "sync_hub_machine_name", None)
    return None if configured is None else str(configured)


@contextmanager
def _hub_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = state_db.open_rw(_db_path(request))
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _require_schema_version(offered: int) -> None:
    if offered != state_schema.SCHEMA_VERSION:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SYNC_SCHEMA_VERSION",
                "message": (
                    f"peer is on schema v{offered}, hub is on "
                    f"v{state_schema.SCHEMA_VERSION}; migrate before syncing."
                ),
            },
        )


def _hub_identity(request: Request, conn: sqlite3.Connection) -> str:
    """Register the hub's own ``machines`` row and return its id."""
    try:
        identity = machine_identity.register_machine(
            conn, data_dir=_data_dir(request), name=_machine_name(request)
        )
    except machine_identity.MachineIdentityError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "SYNC_HUB_IDENTITY", "message": str(exc)},
        ) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SYNC_MACHINE_NAME_TAKEN",
                "message": (
                    f"another machine already registered this hub's name: {exc}. "
                    f"machines.name is UNIQUE; set app.state."
                    f"sync_hub_machine_name to something distinct."
                ),
            },
        ) from exc
    return identity.machine_id


def _machine_models(conn: sqlite3.Connection) -> list[MachineModel]:
    return [
        MachineModel(**machine.to_wire()) for machine in engine.machines_snapshot(conn)
    ]


def _row_models(rows: list[protocol.RowChange]) -> list[RowModel]:
    return [RowModel(**row.to_wire()) for row in rows]


def _to_changes(rows: list[RowModel]) -> list[protocol.RowChange]:
    try:
        return [protocol.RowChange.from_wire(row.model_dump()) for row in rows]
    except protocol.SyncProtocolError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "SYNC_PROTOCOL", "message": str(exc)},
        ) from exc


def _require_registered(conn: sqlite3.Connection, machine_id: str) -> None:
    """Refuse a push from a machine that never said hello.

    Not authentication -- v1 trust is the tailnet (ADR 04 c7). It is a
    consistency guard: an unregistered pusher means the caller skipped the
    handshake, and rows stamped with a device id no ``machines`` row explains
    would make every later conflict unattributable.
    """
    row = conn.execute(
        "SELECT 1 FROM machines WHERE machine_id = ?", (machine_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SYNC_UNKNOWN_MACHINE",
                "message": (
                    f"machine {machine_id} is not registered on this hub; "
                    f"POST /api/v1/sync/hello first."
                ),
            },
        )


def _apply_error(exc: engine.SyncApplyError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": "SYNC_APPLY", "message": str(exc)},
    )


# ----- endpoints -----------------------------------------------------------


@router.post("/hello", response_model=HelloResponse)
def hello(request: Request, payload: HelloRequest) -> HelloResponse:
    """Register a spoke in ``machines`` and report the hub's id and seq."""
    _require_schema_version(payload.schema_version)
    with _hub_conn(request) as conn:
        try:
            with _transaction(conn):
                hub_machine_id = _hub_identity(request, conn)
                engine.merge_machines(
                    conn, [protocol.MachineRow.from_wire(payload.machine.model_dump())]
                )
        except engine.SyncApplyError as exc:
            raise _apply_error(exc) from exc
        except protocol.SyncProtocolError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "SYNC_PROTOCOL", "message": str(exc)},
            ) from exc
        return HelloResponse(
            hub_machine_id=hub_machine_id,
            schema_version=state_schema.SCHEMA_VERSION,
            seq=engine.current_seq(conn),
            machines=_machine_models(conn),
        )


@router.post("/push", response_model=PushResponse)
def push(request: Request, payload: PushRequest) -> PushResponse:
    """Merge offered rows under last-writer-wins; append to ``hub_changelog``."""
    _require_schema_version(payload.schema_version)
    changes = _to_changes(payload.rows)
    with _hub_conn(request) as conn:
        _require_registered(conn, payload.machine_id)
        try:
            with _transaction(conn):
                result = engine.hub_apply(conn, changes)
        except engine.SyncApplyError as exc:
            raise _apply_error(exc) from exc
        return PushResponse(
            accepted=result.accepted, rejected=result.rejected, seq=result.seq
        )


@router.get("/pull", response_model=PullResponse)
def pull(
    request: Request,
    since_seq: int = Query(0, ge=0, description="last hub_changelog.seq applied"),
) -> PullResponse:
    """Rows the hub accepted after ``since_seq``, plus the fleet registry."""
    with _hub_conn(request) as conn:
        try:
            batch = engine.hub_changes_since(conn, since_seq)
        except engine.SyncApplyError as exc:
            raise _apply_error(exc) from exc
        return PullResponse(
            rows=_row_models(batch.rows),
            seq=batch.seq,
            machines=_machine_models(conn),
        )


@router.get("/status", response_model=StatusResponse)
def status(request: Request) -> StatusResponse:
    """Hub identity, current seq, known machines and synced row counts."""
    with _hub_conn(request) as conn:
        with _transaction(conn):
            hub_machine_id = _hub_identity(request, conn)
        counts = {
            name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in protocol.DIGEST_TABLES
        }
        return StatusResponse(
            hub_machine_id=hub_machine_id,
            schema_version=state_schema.SCHEMA_VERSION,
            seq=engine.current_seq(conn),
            machines=_machine_models(conn),
            row_counts=counts,
        )


@router.get("/digest", response_model=DigestResponse)
def digest(request: Request) -> DigestResponse:
    """Per-table digests over the sync set, tombstones included (ADR 04 c6)."""
    with _hub_conn(request) as conn:
        try:
            computed = protocol.sync_digest(conn)
        except protocol.SyncProtocolError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "SYNC_PROTOCOL", "message": str(exc)},
            ) from exc
        return DigestResponse(tables=computed.tables, overall=computed.overall)


__all__ = ["router"]
