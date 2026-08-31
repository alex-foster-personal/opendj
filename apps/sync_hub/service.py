"""The hub side of the sync protocol, as a FastAPI router.

Mounted by the webui at ``/api/v1/sync/*``:

    POST /api/v1/sync/hello    register a spoke, learn the hub id and seq
    POST /api/v1/sync/push     offer rows; the hub merges them under LWW
    GET  /api/v1/sync/pull     one chunk of the rows accepted after ``since_seq``
    GET  /api/v1/sync/status   hub identity, seq, fleet, row counts
    GET  /api/v1/sync/digest   per-table digests for the post-sync compare

Trust in v1 is tailnet membership (ADR 04 c7): any process that can reach the
daemon can push. That is deliberate for a personal fleet and must be revisited
before any multi-user deployment.

``push``, ``pull`` and ``status`` all take a ``machine_id`` and all refuse a
machine that never said hello (ADR 08 point 6, round 1 finding 7b). That is a
consistency guard rather than authentication, and round 1 applied it only to
``push`` -- an asymmetry that looked accidental, since an unregistered
``pull`` returned the entire changelog history plus every machine's absolute
``data_root``.

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
from apps.sync_hub import engine, generation, protocol

router = APIRouter(prefix="/sync", tags=["sync"])

#: Hard ceiling on ``/pull?limit=``. A spoke asking for more than this is
#: asking the hub to hold an unbounded response in memory on its behalf.
MAX_PULL_LIMIT: int = 5000


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
    #: The fleet as the caller knows it (round 2 finding N4). Merged after
    #: ``machine`` so a hub restored to a point before some peer first said
    #: hello learns that peer from whoever still remembers it, instead of
    #: 409ing every row that references it.
    machines: list[MachineModel] = Field(default_factory=list)


class HelloResponse(BaseModel):
    hub_machine_id: str
    schema_version: int
    seq: int
    machines: list[MachineModel]
    #: This hub's generation token (round 2 finding N6). It changes when the
    #: hub's DB moves backwards under a data dir that did not -- a restore --
    #: and NOT when its changelog is pruned. A spoke that sees a different
    #: token than the one it stored resets both sync floors.
    hub_generation: str


class PushRequest(BaseModel):
    machine_id: str = Field(min_length=1)
    schema_version: int
    rows: list[RowModel]
    #: The pusher's ``machines`` snapshot, merged before the rows are applied
    #: (round 2 finding N4, round 1 A4). ``sync_policies``, ``playlist_pins``
    #: and ``track_locations`` all carry a ``machine_id`` REFERENCES
    #: ``machines``, and every spoke holds rows belonging to its peers, so a
    #: hub that has not met one of them refused the whole push with a
    #: FOREIGN KEY 409 that re-fired on every retry. Empty means the caller
    #: offered no snapshot, which is only safe when its rows name machines
    #: this hub already knows.
    machines: list[MachineModel] = Field(default_factory=list)


class PushResponse(BaseModel):
    accepted: int
    rejected: int
    seq: int


class PullResponse(BaseModel):
    rows: list[RowModel]
    seq: int
    machines: list[MachineModel]
    #: True when the hub still holds changelog entries above ``seq``. The
    #: client loops on it rather than inferring "done" from an empty page:
    #: dedup means a chunk can legitimately return fewer rows than entries.
    has_more: bool = False
    #: Changelog entries in this chunk whose row is gone from the hub
    #: (round 2 finding 4a). Non-zero means something hard-deleted a synced
    #: row on the hub; the pull still serves everything else.
    skipped: int = 0


class StatusResponse(BaseModel):
    hub_machine_id: str
    schema_version: int
    seq: int
    machines: list[MachineModel]
    row_counts: dict[str, int]
    hub_generation: str


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


def _to_machines(models: list[MachineModel]) -> list[protocol.MachineRow]:
    try:
        return [protocol.MachineRow.from_wire(model.model_dump()) for model in models]
    except protocol.SyncProtocolError as exc:
        raise _protocol_error(exc) from exc


def _row_models(rows: list[protocol.RowChange]) -> list[RowModel]:
    return [RowModel(**row.to_wire()) for row in rows]


def _to_changes(rows: list[RowModel]) -> list[protocol.RowChange]:
    try:
        return [protocol.RowChange.from_wire(row.model_dump()) for row in rows]
    except protocol.SyncProtocolError as exc:
        raise _protocol_error(exc) from exc


def _protocol_error(exc: protocol.SyncProtocolError) -> HTTPException:
    """422: the payload is not this protocol. Includes an ``updated_at``
    that cannot be placed on the UTC line (ADR 08 point 2)."""
    return HTTPException(
        status_code=422,
        detail={"code": "SYNC_PROTOCOL", "message": str(exc)},
    )


def _require_registered(conn: sqlite3.Connection, machine_id: str) -> None:
    """Refuse a call from a machine that never said hello.

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


def _generation(request: Request, conn: sqlite3.Connection) -> str:
    """This hub's generation token, re-minted if the DB moved backwards.

    Called OUTSIDE the writing transaction on purpose
    (:func:`apps.sync_hub.generation.observe`): an anchor written inside a
    transaction that then rolled back would sit above the hub's seq, and
    every later call would read that as a restore.
    """
    try:
        return generation.observe(conn, _data_dir(request))
    except generation.SyncGenerationError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "SYNC_HUB_GENERATION", "message": str(exc)},
        ) from exc


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
                    conn,
                    [protocol.MachineRow.from_wire(payload.machine.model_dump())]
                    + _to_machines(payload.machines),
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
            hub_generation=_generation(request, conn),
        )


@router.post("/push", response_model=PushResponse)
def push(request: Request, payload: PushRequest) -> PushResponse:
    """Merge offered rows under last-writer-wins; append to ``hub_changelog``.

    The pusher's ``machines`` snapshot is merged first, in the same
    transaction (round 2 finding N4): a row this hub has never met is a
    FOREIGN KEY violation, and the recovery push after a hub restore is
    exactly the push most likely to carry one.
    """
    _require_schema_version(payload.schema_version)
    changes = _to_changes(payload.rows)
    fleet = _to_machines(payload.machines)
    with _hub_conn(request) as conn:
        _require_registered(conn, payload.machine_id)
        try:
            with _transaction(conn):
                engine.merge_machines(conn, fleet)
                result = engine.hub_apply(conn, changes)
        except engine.SyncApplyError as exc:
            raise _apply_error(exc) from exc
        except protocol.SyncProtocolError as exc:
            raise _protocol_error(exc) from exc
        # After the COMMIT: the anchor records the greatest seq this hub has
        # ever reported, and it must never sit above what the DB holds.
        _generation(request, conn)
        return PushResponse(
            accepted=result.accepted, rejected=result.rejected, seq=result.seq
        )


@router.get("/pull", response_model=PullResponse)
def pull(
    request: Request,
    machine_id: str = Query(min_length=1, description="the calling spoke"),
    since_seq: int = Query(0, ge=0, description="last hub_changelog.seq applied"),
    limit: int = Query(
        engine.DEFAULT_PULL_LIMIT,
        ge=1,
        le=MAX_PULL_LIMIT,
        description="max changelog entries to consume in this chunk",
    ),
) -> PullResponse:
    """One chunk of the rows the hub accepted after ``since_seq``.

    Chunked because a first sync of a real library is megabytes of JSON held
    twice in memory on both sides (round 1 finding A2). ``has_more`` tells
    the client to come back with the ``seq`` this response reports.
    """
    with _hub_conn(request) as conn:
        _require_registered(conn, machine_id)
        try:
            batch = engine.hub_changes_since(conn, since_seq, limit=limit)
        except engine.SyncApplyError as exc:
            raise _apply_error(exc) from exc
        except protocol.SyncProtocolError as exc:
            raise _protocol_error(exc) from exc
        return PullResponse(
            rows=_row_models(batch.rows),
            seq=batch.seq,
            machines=_machine_models(conn),
            has_more=batch.has_more,
            skipped=batch.skipped,
        )


@router.get("/status", response_model=StatusResponse)
def status(
    request: Request,
    machine_id: str = Query(min_length=1, description="the calling spoke"),
) -> StatusResponse:
    """Hub identity, current seq, known machines and synced row counts."""
    with _hub_conn(request) as conn:
        with _transaction(conn):
            hub_machine_id = _hub_identity(request, conn)
        _require_registered(conn, machine_id)
        # One read transaction so the counts, the seq and the fleet all
        # describe the same instant rather than three consecutive ones.
        with _transaction(conn):
            counts = {
                name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                for name in protocol.DIGEST_TABLES
            }
            seq = engine.current_seq(conn)
            machines = _machine_models(conn)
        return StatusResponse(
            hub_machine_id=hub_machine_id,
            schema_version=state_schema.SCHEMA_VERSION,
            seq=seq,
            machines=machines,
            row_counts=counts,
            hub_generation=_generation(request, conn),
        )


@router.get("/digest", response_model=DigestResponse)
def digest(request: Request) -> DigestResponse:
    """Per-table digests over the sync set, tombstones included (ADR 04 c6).

    Computed inside one read transaction (ADR 08 point 6b): without it a
    table read late in the walk can include a push that landed after an
    earlier table was read, and the answer describes a hub state that never
    existed. It still answers as of request time, so a spoke comparing
    against its own strictly earlier commit can see a legitimate difference
    if a third machine pushes in the gap; narrowing that window further is a
    protocol change (a ``seq`` parameter) the ADR did not take.
    """
    with _hub_conn(request) as conn:
        try:
            with _transaction(conn):
                computed = protocol.sync_digest(conn)
        except protocol.SyncProtocolError as exc:
            raise _protocol_error(exc) from exc
        return DigestResponse(tables=computed.tables, overall=computed.overall)


__all__ = ["router"]
