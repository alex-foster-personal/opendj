"""The hub side of the sync protocol, as a FastAPI router.

Mounted by the webui at ``/api/v1/sync/*``:

    POST /api/v1/sync/hello    register a spoke, learn the hub id and seq
    POST /api/v1/sync/enroll   join the fleet under a proved owner (ADR 12)
    POST /api/v1/sync/push     offer rows; the hub merges them under LWW
    GET  /api/v1/sync/pull     one chunk of the rows accepted after ``since_seq``
    GET  /api/v1/sync/status   hub identity, seq, fleet, row counts
    GET  /api/v1/sync/digest   per-table digests for the post-sync compare

Machines authenticate with a per-machine sync credential (plan X5), minted by
``/enroll`` (ADR 12) and checked on ``hello``, ``push``, ``pull``, ``status``
and ``digest`` by :mod:`apps.sync_hub.service_credentials`. The default
OBSERVE mode refuses nothing (it logs the verdict and ``hello`` reports it);
only ``MDT_SYNC_CREDENTIAL_MODE=enforce`` answers 401. Until then trust is
tailnet membership (ADR 04 c7), and reading this file as "the hub is
authenticated" would be exactly the error ``.claude/rules/verification.md``
is about. Multi-user: ``specs/cloudsync-multi-user.md``.

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

**Partial answers are gated on one invariant** (round 5 gate B-1): *this hub
answers a caller that has not advertised ``quarantine/v1`` exactly as
``origin/main`` would* -- 422 ``SYNC_PROTOCOL``, nothing partial, nothing
committed. Round 5 turned "the hub cannot order its own copy of this row"
from a 422 into a 200 reporting a shortfall, which an ``origin/main`` spoke
cannot see: it stamps ``last_push_seq = ceiling`` regardless, the held row
falls below a fence that can never select it again, and the hub -- having
written no ``hub_changelog`` entry -- cannot re-deliver it. Upgrading the hub
first would therefore delete data on every spoke still on main. The three
endpoints that can answer partially (``push``, ``pull``, ``digest``) each ask
:mod:`apps.sync_hub.capabilities` first, and the advertisement is read off
the REQUEST rather than remembered from ``hello``: a spoke rolled back to an
older build must stop being capable the moment it rolls back.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.shared.state import schema as state_schema
from apps.sync_hub import (
    capabilities,
    engine,
    enrollment,
    entitlement_gate,
    generation,
    protocol,
    service_credentials,
    service_enroll,
    service_shortfall,
    service_storage,
    wire_version,
)
from apps.sync_hub.service_models import (
    SYNC_VERSION_RESPONSES,
    DigestResponse,
    EnrollRequest,
    HelloRequest,
    HelloResponse,
    MachineModel,
    PullResponse,
    PushRequest,
    PushResponse,
    RowModel,
    StatusResponse,
)

router = APIRouter(prefix="/sync", tags=["sync"])
#: The 401/503 each credential-gated route declares, per endpoint (plan X5).
_auth = service_credentials.credential_responses

#: Hard ceiling on ``/pull?limit=``. A spoke asking for more than this is
#: asking the hub to hold an unbounded response in memory on its behalf.
MAX_PULL_LIMIT: int = 5000

#: The ``?capabilities=`` query the two GET endpoints that can answer
#: partially both declare. A module-level singleton rather than a ``Query()``
#: call in each signature: ruff B008 flags the call-in-default for a MUTABLE
#: annotation like ``list[str]``, and one shared declaration is the DRY
#: answer as well as the lint-clean one. The parameter is spelled
#: ``capabilities_`` in Python and ``capabilities`` on the wire, so the name
#: cannot collide with the :mod:`apps.sync_hub.capabilities` module.
_CAPABILITIES_QUERY: Any = Query(
    default_factory=list,
    alias="capabilities",
    description="protocol features the caller understands",
)


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


def _require_same_wire(offered_wire: int | None, offered_schema: int) -> None:
    """409 before any row is read or written; the rule is in :mod:`wire_version`."""
    refusal = wire_version.incompatibility(offered_wire, offered_schema, peer="peer")
    if refusal is not None:
        raise HTTPException(
            status_code=409, detail={"code": refusal.code, "message": refusal.message}
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


def _require_credential(
    request: Request, conn: sqlite3.Connection, machine_id: str, endpoint: str
) -> service_credentials.CredentialVerdict:
    """Read-only; call BEFORE ``_require_registered`` and before any write."""
    return service_credentials.require_credential(
        request, conn, machine_id, data_dir=_data_dir(request), endpoint=endpoint
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


def _gate(
    request: Request, conn: sqlite3.Connection, machine_id: str, op: entitlement_gate.Operation
) -> None:
    """The hosted-hub entitlement check; returns at once on a self-hosted hub."""
    entitlement_gate.require(
        request, conn, machine_id=machine_id, operation=op, data_dir=lambda: _data_dir(request)
    )


def _apply_error(exc: engine.SyncApplyError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": "SYNC_APPLY", "message": str(exc)},
    )


# ----- the partial-answer gate (round 5 gate B-1) --------------------------


def _refuse_unless_capable(
    advertised: Sequence[str], endpoint: str, detail: str | None
) -> None:
    """Refuse a partial answer to a caller that cannot read one.

    ``detail`` is ``None`` when this request held nothing back, which is the
    overwhelmingly common case and the only one that costs nothing: the gate
    asks about rows the hub ALREADY decided to hold, so it never re-measures
    anything and never fires on healthy data.

    Raises :class:`apps.sync_hub.protocol.SyncProtocolError` rather than an
    ``HTTPException`` so that every caller converts it through the existing
    :func:`_protocol_error` and answers with the SAME 422 ``SYNC_PROTOCOL``
    body ``origin/main`` answered with -- and, inside ``push``, so the raise
    lands in the open transaction and rolls the whole batch back exactly as
    main's mid-apply raise did.
    """
    if detail is None:
        return
    if capabilities.understands_quarantine(advertised):
        return
    raise protocol.SyncProtocolError(
        capabilities.refusal(endpoint, detail, advertised)
    )


# ----- endpoints -----------------------------------------------------------


@router.post(
    "/hello",
    response_model=HelloResponse,
    responses={**SYNC_VERSION_RESPONSES, **_auth("hello")},
)
def hello(request: Request, payload: HelloRequest) -> HelloResponse:
    """Register a spoke in ``machines`` and report the hub's id and seq."""
    _require_same_wire(payload.wire_version, payload.schema_version)
    with _hub_conn(request) as conn:
        credential = _require_credential(request, conn, payload.machine.machine_id, "hello")
        try:
            with _transaction(conn):
                hub_machine_id = _hub_identity(request, conn)
                # The spoke authored this snapshot, so it may refresh its OWN
                # row (payload.machine) and only teach the hub about the rest
                # of its fleet -- never rewrite a peer's row (round 3 R1).
                engine.merge_machines(
                    conn,
                    [
                        protocol.MachineRow.from_wire(payload.machine.model_dump()),
                        *_to_machines(payload.machines),
                    ],
                    caller_id=payload.machine.machine_id,
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
            wire_version=wire_version.WIRE_VERSION,
            seq=engine.current_seq(conn),
            machines=_machine_models(conn),
            hub_generation=_generation(request, conn),
            capabilities=list(capabilities.THIS_BUILD),
            ownership=enrollment.ownership_state(
                conn, payload.machine.machine_id, hub_machine_id=hub_machine_id
            ),
            credential=credential,
        )


@router.post(
    "/enroll",
    response_model=service_enroll.EnrollResponse,
    responses=service_enroll.ENROLL_RESPONSES,
)
def enroll(
    request: Request, payload: EnrollRequest
) -> service_enroll.EnrollResponse:
    """Join this hub's fleet under a proved owner. The ONE enrollment door.

    Both ADR 12 paths come through here and neither can reach
    :func:`apps.sync_hub.enrollment.enroll_machine` any other way: the dev
    CLI is a thin argparse shell over this exact call, and the user path will
    be the same call carrying a different credential kind.

    Deliberately NOT behind a router-level dependency. It authenticates by
    the credential in its BODY, which is what lets a headless machine with no
    browser and no local Google session enroll at all. One transaction, so a
    refusal registers nothing. Idempotent: a re-run answers ``created: false``.
    """
    _require_same_wire(payload.wire_version, payload.schema_version)
    machine = _to_machines([payload.machine])[0]
    # Before BEGIN: verification may fetch Google's JWKS, and an
    # unauthenticated caller must never hold the write lock across that.
    credential = service_enroll.verify_credential(payload.credential)
    with _hub_conn(request) as conn:
        try:
            with _transaction(conn):
                return service_enroll.perform_enroll(
                    conn,
                    machine=machine,
                    credential=credential,
                    hub_machine_id=_hub_identity(request, conn),
                )
        except engine.SyncApplyError as exc:
            raise _apply_error(exc) from exc


@router.post(
    "/push",
    response_model=PushResponse,
    responses={
        **service_storage.PUSH_STORAGE_RESPONSES,
        **_auth("push"),
        **SYNC_VERSION_RESPONSES,
        **entitlement_gate.refusals("push"),
    },
)
def push(request: Request, payload: PushRequest) -> PushResponse:
    """Merge offered rows under last-writer-wins; append to ``hub_changelog``.

    The pusher's ``machines`` snapshot is merged first, in the same
    transaction (round 2 finding N4): a row this hub has never met is a
    FOREIGN KEY violation, and the recovery push after a hub restore is
    exactly the push most likely to carry one.

    A pusher that did not advertise ``quarantine/v1`` gets ``origin/main``'s
    answer instead of the partial one: 422, whole batch rolled back (module
    docstring). That is the staged-rollout price and it is the safe half of
    it -- an un-upgraded spoke reads ``accepted + rejected < offered`` as
    nothing at all and steps its push fence over the held row.
    """
    _require_same_wire(payload.wire_version, payload.schema_version)
    changes = _to_changes(payload.rows)
    fleet = _to_machines(payload.machines)
    with _hub_conn(request) as conn:
        _require_credential(request, conn, payload.machine_id, "push")
        _require_registered(conn, payload.machine_id)
        _gate(request, conn, payload.machine_id, "write")
        try:
            with _transaction(conn):
                engine.merge_machines(conn, fleet, caller_id=payload.machine_id)
                result = engine.hub_apply(conn, changes)
                # INSIDE the transaction: a refusal must roll the whole batch
                # back, which is what main's mid-apply raise did and what the
                # refused caller's retry is entitled to assume.
                _refuse_unless_capable(
                    payload.capabilities,
                    "push",
                    service_shortfall.push_shortfall(result, len(changes)),
                )
            # After COMMIT: observe must stay outside the writing transaction
            # (existing comment on _generation). Catch OperationalError here
            # too so a SQLITE_FULL on current_seq is 507, not a bare 500.
            _generation(request, conn)
        except engine.SyncApplyError as exc:
            raise _apply_error(exc) from exc
        except protocol.SyncProtocolError as exc:
            raise _protocol_error(exc) from exc
        except sqlite3.OperationalError as exc:
            service_storage.raise_for_operational_error(exc)
        return PushResponse(
            accepted=result.accepted,
            rejected=result.rejected,
            seq=result.seq,
            quarantined=result.quarantined,
        )


@router.get(
    "/pull",
    response_model=PullResponse,
    responses={**_auth("pull"), **entitlement_gate.refusals("pull")},
)
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
    capabilities_: list[str] = _CAPABILITIES_QUERY,
) -> PullResponse:
    """One chunk of the rows the hub accepted after ``since_seq``.

    Chunked because a first sync of a real library is megabytes of JSON held
    twice in memory on both sides (round 1 finding A2). ``has_more`` tells
    the client to come back with the ``seq`` this response reports.

    A chunk that had to leave a row out is refused outright for a caller that
    did not advertise ``quarantine/v1`` (module docstring). The shortfall is
    invisible to such a caller, which records the reported ``seq`` as pulled
    and can never ask for those entries again -- not even after the repair.
    """
    with _hub_conn(request) as conn:
        _require_credential(request, conn, machine_id, "pull")
        _require_registered(conn, machine_id)
        _gate(request, conn, machine_id, "read")
        try:
            batch = engine.hub_changes_since(conn, since_seq, limit=limit)
            _refuse_unless_capable(capabilities_, "pull", service_shortfall.pull_shortfall(batch))
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
            quarantined=batch.quarantined,
        )


@router.get("/status", response_model=StatusResponse, responses=_auth("status"))
def status(
    request: Request,
    machine_id: str = Query(min_length=1, description="the calling spoke"),
) -> StatusResponse:
    """Hub identity, current seq, known machines and synced row counts."""
    with _hub_conn(request) as conn:
        # Refuse BEFORE the hub identity write (round 3 finding R8): the old
        # order registered the hub's own machines row in a transaction that
        # committed regardless of whether the CALLER turned out to be
        # unregistered, so a refused status call still had a write side
        # effect. ``_require_registered`` only reads, so this costs nothing
        # on the accepted path and nothing happens at all on the refused one.
        _require_credential(request, conn, machine_id, "status")
        _require_registered(conn, machine_id)
        with _transaction(conn):
            hub_machine_id = _hub_identity(request, conn)
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
            wire_version=wire_version.WIRE_VERSION,
            seq=seq,
            machines=machines,
            row_counts=counts,
            hub_generation=_generation(request, conn),
            **entitlement_gate.status_fields(request),
        )


@router.get("/digest", response_model=DigestResponse, responses=_auth("digest"))
def digest(
    request: Request,
    machine_id: str = Query(min_length=1, description="the calling spoke"),
    capabilities_: list[str] = _CAPABILITIES_QUERY,
) -> DigestResponse:
    """Per-table digests over the sync set, tombstones included (ADR 04 c6).

    Computed inside one read transaction (ADR 08 point 6b): without it a
    table read late in the walk can include a push that landed after an
    earlier table was read, and the answer describes a hub state that never
    existed.

    It answers as of request time and SAYS SO: ``seq`` is the changelog
    position of that answer, read in the same transaction (round 2 finding
    6b). The hub cannot answer "as of seq N" -- the changelog records that a
    row changed, never what it held, so there is no earlier state to
    reconstruct. Reporting the position instead lets the spoke tell a third
    machine's push apart from a real divergence, and settle by pulling
    again rather than halting.

    Requires registration like every other endpoint (round 1 finding 7b,
    closed everywhere except here until round 3 finding R8): unlike
    ``/pull``, this answer carries no per-row data, but it does carry the
    hub's live changelog position, which an unregistered caller had no
    business reading either.

    A hub holding one row with an unorderable stored stamp no longer answers
    422 (round 5). That row is excluded from the hash and counted in
    ``quarantined``: a legacy row is a fact to report, not a reason to make
    the endpoint every sync depends on unavailable.

    For a caller that did not advertise ``quarantine/v1`` it still does
    (module docstring). Such a spoke has no ``quarantined`` map to read, so
    it compares a hash over the eligible set against its own hash over
    everything, and the difference reads to it as the ADR 04 c6 CORRUPTION
    alarm -- a false one, raised on ordinary legacy data.
    """
    with _hub_conn(request) as conn:
        _require_credential(request, conn, machine_id, "digest")
        _require_registered(conn, machine_id)
        try:
            with _transaction(conn):
                computed = protocol.sync_digest(conn, seq=engine.current_seq(conn))
            _refuse_unless_capable(
                capabilities_, "digest", service_shortfall.digest_shortfall(computed)
            )
        except protocol.SyncProtocolError as exc:
            raise _protocol_error(exc) from exc
        return DigestResponse(
            tables=computed.tables,
            overall=computed.overall,
            seq=computed.seq,
            quarantined=dict(computed.quarantined or {}),
        )


__all__ = [
    "DigestResponse",
    "EnrollRequest",
    "HelloRequest",
    "HelloResponse",
    "MachineModel",
    "PullResponse",
    "PushRequest",
    "PushResponse",
    "RowModel",
    "StatusResponse",
    "router",
]
