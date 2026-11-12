"""Shared wiring helpers for hub sync route handlers.

Split out of :mod:`apps.sync_hub.service` so credential lookup, hub DB
connections, transaction boundaries and the partial-answer gate live in one
place the handshake, push and read routers import.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException, Query, Request

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.sync_hub import (
    capabilities,
    engine,
    entitlement_gate,
    generation,
    hub_wal_keeper,
    protocol,
    service_credentials,
    wire_version,
)
from apps.sync_hub.machine_wire_limits import clamp_machine_row_for_wire
from apps.sync_hub.service_models import MachineModel, RowModel
from apps.sync_hub.service_request import _data_dir, _db_path

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


def _machine_name(request: Request) -> str | None:
    configured = getattr(request.app.state, "sync_hub_machine_name", None)
    return None if configured is None else str(configured)


@contextmanager
def _hub_conn(request: Request) -> Iterator[sqlite3.Connection]:
    path = _db_path(request)
    conn = state_db.open_rw(path)
    try:
        # Opened after open_rw has created and migrated the DB, so this
        # close() never checkpoints and deletes the WAL (LIBM-120 L6).
        hub_wal_keeper.keep_wal_open(request.app.state, path)
        yield conn
    finally:
        conn.close()


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    # IMMEDIATE, not a deferred BEGIN: these transactions read before they write
    # (register_machine checks the name before its upsert, #8314), and a deferred
    # read lock that must upgrade under a racing writer fails at once with
    # "database is locked" instead of waiting on the busy timeout.
    conn.execute("BEGIN IMMEDIATE")
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
        raise HTTPException(status_code=409, detail={"code": refusal.code, "message": refusal.message})


def _hub_identity(request: Request, conn: sqlite3.Connection) -> str:
    """Register the hub's own ``machines`` row and return its id."""
    try:
        identity = machine_identity.register_machine(conn, data_dir=_data_dir(request), name=_machine_name(request))
    except machine_identity.MachineNameConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "SYNC_MACHINE_NAME_TAKEN", "message": str(exc)},
        ) from exc
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
    return [MachineModel(**clamp_machine_row_for_wire(machine).to_wire()) for machine in engine.machines_snapshot(conn)]


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
    row = conn.execute("SELECT 1 FROM machines WHERE machine_id = ?", (machine_id,)).fetchone()
    if row is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SYNC_UNKNOWN_MACHINE",
                "message": (f"machine {machine_id} is not registered on this hub; POST /api/v1/sync/hello first."),
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


def _gate(request: Request, conn: sqlite3.Connection, machine_id: str, op: entitlement_gate.Operation) -> None:
    """The hosted-hub entitlement check; returns at once on a self-hosted hub."""
    entitlement_gate.require(request, conn, machine_id=machine_id, operation=op, data_dir=lambda: _data_dir(request))


def _apply_error(exc: engine.SyncApplyError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": "SYNC_APPLY", "message": str(exc)},
    )


# ----- the partial-answer gate (round 5 gate B-1) --------------------------


def _refuse_unless_capable(advertised: Sequence[str], endpoint: str, detail: str | None) -> None:
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
    raise protocol.SyncProtocolError(capabilities.refusal(endpoint, detail, advertised))


def _refuse_hash_pending_unless_capable(
    changes: Sequence[protocol.RowChange],
    advertised: Sequence[str],
    endpoint: str,
) -> None:
    """Refuse a batch carrying ``hash_pending`` rows the caller cannot read."""
    pending = sum(1 for change in changes if change.hash_pending)
    if not pending:
        return
    if capabilities.understands_hash_pending(advertised):
        return
    raise protocol.SyncProtocolError(capabilities.hash_pending_refusal(endpoint, pending, advertised))


__all__ = [
    "MAX_PULL_LIMIT",
    "_CAPABILITIES_QUERY",
    "_apply_error",
    "_auth",
    "_gate",
    "_generation",
    "_hub_conn",
    "_hub_identity",
    "_machine_models",
    "_machine_name",
    "_protocol_error",
    "_refuse_hash_pending_unless_capable",
    "_refuse_unless_capable",
    "_require_credential",
    "_require_registered",
    "_require_same_wire",
    "_row_models",
    "_to_changes",
    "_to_machines",
    "_transaction",
]
