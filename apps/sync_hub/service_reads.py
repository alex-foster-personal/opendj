"""Pull, status, digest, rows and hash-pending hub sync endpoints.

Split out of :mod:`apps.sync_hub.service`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query, Request

from apps.shared.state import schema as state_schema
from apps.sync_hub import (
    capabilities,
    digest_diff,
    engine,
    entitlement_gate,
    protocol,
    r2_capability,
    service_shortfall,
    wire_version,
)
from apps.sync_hub import (
    hash_pending as hash_pending_api,
)
from apps.sync_hub.machine_wire_limits import MACHINE_ID_MAX_LENGTH
from apps.sync_hub.service_models import (
    DigestResponse,
    HashPendingResponse,
    PullResponse,
    R2CapabilityModel,
    RowsResponse,
    StatusResponse,
    SyncRowSampleModel,
)
from apps.sync_hub.service_wiring import (
    _CAPABILITIES_QUERY,
    MAX_PULL_LIMIT,
    _apply_error,
    _auth,
    _data_dir,
    _gate,
    _generation,
    _hub_conn,
    _hub_identity,
    _machine_models,
    _protocol_error,
    _refuse_unless_capable,
    _require_credential,
    _require_registered,
    _row_models,
    _transaction,
)

router = APIRouter()


@router.get(
    "/pull",
    response_model=PullResponse,
    responses=entitlement_gate.merge_responses(_auth("pull"), entitlement_gate.refusals("pull")),
)
def pull(
    request: Request,
    machine_id: str = Query(min_length=1, max_length=MACHINE_ID_MAX_LENGTH, description="the calling spoke"),
    since_seq: int = Query(0, ge=0, description="last hub_changelog.seq applied"),
    limit: int = Query(
        engine.DEFAULT_PULL_LIMIT,
        ge=1,
        le=MAX_PULL_LIMIT,
        description="max changelog entries to consume in this chunk",
    ),
    bundle_stable_ids: list[str] = Query(  # noqa: B008  # FastAPI DI
        default=[],
        description=(
            "optional track stable_id values whose live bundles are appended "
            "for identity repair without advancing the changelog cursor"
        ),
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
            if bundle_stable_ids:
                rows = engine.hub_track_bundles(conn, bundle_stable_ids)
                seq = since_seq
                has_more = False
                skipped = 0
                quarantined = 0
            else:
                batch = engine.hub_changes_since(conn, since_seq, limit=limit)
                rows = batch.rows
                seq = batch.seq
                has_more = batch.has_more
                skipped = batch.skipped
                quarantined = batch.quarantined
                _refuse_unless_capable(
                    capabilities_,
                    "pull",
                    service_shortfall.pull_shortfall(batch, conn),
                )
        except engine.SyncApplyError as exc:
            raise _apply_error(exc) from exc
        except protocol.SyncProtocolError as exc:
            raise _protocol_error(exc) from exc
        return PullResponse(
            rows=_row_models(rows),
            seq=seq,
            machines=_machine_models(conn),
            has_more=has_more,
            skipped=skipped,
            quarantined=quarantined,
        )


def _r2_capability(request: Request, conn: sqlite3.Connection) -> R2CapabilityModel | None:
    """Measured presign capability; None unless this process is a hub."""
    try:
        capability = r2_capability.capability_for_hub(request.app.state, conn, _data_dir(request))
    except r2_capability.R2CapabilityError as exc:
        raise HTTPException(status_code=500, detail={"code": "SYNC_HUB_R2_CAPABILITY", "message": str(exc)}) from exc
    return None if capability is None else R2CapabilityModel(**asdict(capability))


@router.get("/status", response_model=StatusResponse, responses=_auth("status"))
def status(
    request: Request,
    machine_id: str = Query(min_length=1, max_length=MACHINE_ID_MAX_LENGTH, description="the calling spoke"),
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
                name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]) for name in protocol.DIGEST_TABLES
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
            r2=_r2_capability(request, conn),
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
            _refuse_unless_capable(capabilities_, "digest", service_shortfall.digest_shortfall(computed, conn))
        except protocol.SyncProtocolError as exc:
            raise _protocol_error(exc) from exc
        return DigestResponse(
            tables=computed.tables,
            overall=computed.overall,
            seq=computed.seq,
            quarantined=dict(computed.quarantined or {}),
            hash_pending=dict(computed.hash_pending or {}),
        )


@router.get("/rows", response_model=RowsResponse, responses=_auth("rows"))
def rows(
    request: Request,
    machine_id: str = Query(min_length=1, description="the calling spoke"),
    table: str = Query(min_length=1, description="sync-set table name"),
    limit: int = Query(500, ge=1, le=5000),
    cursor: str | None = Query(default=None),
    _capabilities_: list[str] = _CAPABILITIES_QUERY,  # noqa: ARG001 - OpenAPI query only
) -> RowsResponse:
    """Paginated sync-eligible canonical rows for post-sync diff (CSSTATUS-09)."""
    if table not in protocol.DIGEST_TABLES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SYNC_PROTOCOL",
                "message": f"table {table!r} is not in the sync digest set",
            },
        )
    with _hub_conn(request) as conn:
        _require_credential(request, conn, machine_id, "rows")
        _require_registered(conn, machine_id)
        try:
            with _transaction(conn):
                page, next_cursor = digest_diff.hub_sync_row_page(conn, table, cursor=cursor, limit=limit)
        except protocol.SyncProtocolError as exc:
            raise _protocol_error(exc) from exc
        return RowsResponse(
            rows=[
                SyncRowSampleModel(
                    pk=list(sample.pk),
                    canonical_hex=sample.canonical_hex,
                    updated_at=sample.updated_at,
                    origin_device_id=sample.origin_device_id,
                    modified_at=sample.modified_at,
                )
                for sample in page
            ],
            next_cursor=next_cursor,
        )


@router.get(
    "/hash-pending",
    response_model=HashPendingResponse,
    responses=_auth("hash-pending"),
)
def hash_pending_list(
    request: Request,
    machine_id: str = Query(min_length=1, description="the calling spoke"),
    limit: int = Query(500, ge=1, le=5000),
    cursor: str | None = Query(default=None),
    capabilities_: list[str] = _CAPABILITIES_QUERY,
) -> HashPendingResponse:
    """List ``stable_id`` values on this hub still awaiting ``content_hash``."""
    if not capabilities.understands_hash_pending(capabilities_):
        raise _protocol_error(
            protocol.SyncProtocolError(capabilities.hash_pending_refusal("hash-pending", 1, capabilities_))
        )
    with _hub_conn(request) as conn:
        _require_credential(request, conn, machine_id, "hash-pending")
        _require_registered(conn, machine_id)
        page = hash_pending_api.list_hash_pending(conn, limit=limit, cursor=cursor)
        return HashPendingResponse(
            stable_ids=list(page.stable_ids),
            total=page.total,
            next_cursor=page.next_cursor,
            schema_version=state_schema.SCHEMA_VERSION,
            wire_version=wire_version.WIRE_VERSION,
        )


__all__ = [
    "digest",
    "hash_pending_list",
    "pull",
    "router",
    "rows",
    "status",
]
