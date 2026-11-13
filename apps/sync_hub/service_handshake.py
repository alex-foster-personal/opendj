"""Hello and enroll hub sync endpoints.

Split out of :mod:`apps.sync_hub.service`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from apps.shared.state import schema as state_schema
from apps.shared.state import sync_stamp
from apps.sync_hub import capabilities, engine, enrollment, protocol, service_enroll, wire_version
from apps.sync_hub.service_models import (
    SYNC_VERSION_RESPONSES,
    EnrollRequest,
    HelloRequest,
    HelloResponse,
)
from apps.sync_hub.service_wiring import (
    _apply_error,
    _auth,
    _generation,
    _hub_conn,
    _hub_identity,
    _machine_models,
    _require_credential,
    _require_same_wire,
    _to_machines,
    _transaction,
)

router = APIRouter()


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
                engine.retire_tombstoned_remaps(conn, sync_stamp.canonical_now())
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
            ownership=enrollment.ownership_state(conn, payload.machine.machine_id, hub_machine_id=hub_machine_id),
            credential=credential,
            library_track_count=engine.hub_library_size(conn),
        )


@router.post(
    "/enroll",
    response_model=service_enroll.EnrollResponse,
    responses=service_enroll.ENROLL_RESPONSES,
)
def enroll(request: Request, payload: EnrollRequest) -> service_enroll.EnrollResponse:
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
        # The loser of a race on one grant must wait out the winner and get
        # its 401, not a lock timeout (#9684).
        service_enroll.wait_longer_for_enroll_lock(conn)
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


__all__ = ["enroll", "hello", "router"]
