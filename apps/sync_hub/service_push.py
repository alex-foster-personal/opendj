"""Push and stale-check hub sync endpoints.

Split out of :mod:`apps.sync_hub.service`.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Request

from apps.sync_hub import (
    engine,
    entitlement_gate,
    identity_remap_precondition,
    policy_push,
    policy_store,
    protocol,
    service_shortfall,
    service_storage,
    stale_copy,
)
from apps.sync_hub.service_models import (
    SYNC_VERSION_RESPONSES,
    IdentityRejectModel,
    IdentityRemapPairModel,
    PushRequest,
    PushResponse,
    RejectedRowModel,
    StaleCheckRequest,
    StaleCheckResponse,
)
from apps.sync_hub.service_wiring import (
    _apply_error,
    _auth,
    _gate,
    _generation,
    _hub_conn,
    _protocol_error,
    _refuse_hash_pending_unless_capable,
    _refuse_unless_capable,
    _require_credential,
    _require_registered,
    _require_same_wire,
    _to_changes,
    _to_machines,
    _transaction,
)

router = APIRouter()


def _policy_push_error(exc: policy_push.SyncPolicyViolationError) -> HTTPException:
    """422: offered policy rows break a blocking rule."""
    blocking = [v for v in exc.outcome.violations if v.blocking]
    message = blocking[0].message if blocking else "policy violation on push batch"
    return HTTPException(
        status_code=422,
        detail={
            "code": "SYNC_POLICY_VIOLATION",
            "message": message,
            "violations": policy_push.blocking_violations_wire(exc.outcome),
        },
    )


def _stale_tracks_error(verdict: stale_copy.Verdict) -> HTTPException:
    """409: the push would bring back tracks the fleet dropped (#4628)."""
    examples = list(verdict.orphans[: stale_copy.EXAMPLES])
    return HTTPException(
        status_code=409,
        detail={
            "code": stale_copy.CODE,
            "message": stale_copy.refusal_message(len(verdict.orphans), examples),
            "count": len(verdict.orphans),
            "examples": examples,
        },
    )


@router.post(
    "/push",
    response_model=PushResponse,
    responses={
        **entitlement_gate.merge_responses(
            service_storage.PUSH_STORAGE_RESPONSES,
            _auth("push"),
            SYNC_VERSION_RESPONSES,
            entitlement_gate.refusals("push"),
        ),
        # 409 is restated whole below: its text already names the version refusals.
        409: {
            "description": (
                f"{stale_copy.CODE}: the batch carries live tracks another "
                "machine authored that this hub no longer holds (a stale or "
                "copied library). Nothing applied. Also SYNC_WIRE_VERSION / "
                "SYNC_SCHEMA_VERSION and FOREIGN KEY refusals."
            ),
        },
        422: {
            "description": (
                "SYNC_PROTOCOL (stamp/capability gate) or SYNC_POLICY_VIOLATION "
                "(blocking policy rule on offered sync_policies / playlist_pins rows)"
            ),
        },
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
                _refuse_hash_pending_unless_capable(changes, payload.capabilities, "push")
                engine.merge_machines(conn, fleet, caller_id=payload.machine_id)
                policy_push.evaluate_push_policies(conn, payload.machine_id, changes)
                stale_copy.refuse_orphans(conn, changes, payload.machine_id, reseed=payload.reseed)
                contradicted = identity_remap_precondition.contradicted_pairs(
                    conn, [(pair.loser_pk, pair.survivor_pk) for pair in payload.identity_remap_pairs or ()]
                )
                kept, rejected = identity_remap_precondition.split_offer(changes, contradicted)
                result = identity_remap_precondition.merge_apply_result(engine.hub_apply(conn, kept), rejected)
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
        except policy_push.SyncPolicyViolationError as exc:
            raise _policy_push_error(exc) from exc
        except stale_copy.StaleTracksRefusedError as exc:
            raise _stale_tracks_error(exc.verdict) from exc
        except policy_store.PolicyInputError as exc:
            raise HTTPException(
                status_code=exc.status,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except protocol.SyncProtocolError as exc:
            raise _protocol_error(exc) from exc
        except sqlite3.OperationalError as exc:
            service_storage.raise_for_operational_error(exc)
        return PushResponse(
            identity_remaps_contradicted=[
                IdentityRemapPairModel(loser_pk=loser, survivor_pk=survivor) for loser, survivor in contradicted
            ],
            accepted=result.accepted,
            rejected=result.rejected,
            seq=result.seq,
            quarantined=result.quarantined,
            hash_pending=result.hash_pending,
            identity_rejects=[
                IdentityRejectModel(
                    table=reject.table,
                    offered_pk=reject.offered_pk,
                    survivor_pk=reject.survivor_pk,
                )
                for reject in result.identity_rejects
            ],
            rejected_rows=[
                RejectedRowModel(table=row.table, pk=list(row.pk), reason=row.reason) for row in result.rejected_rows
            ],
        )


@router.post(
    "/stale-check",
    response_model=StaleCheckResponse,
    responses={**_auth("stale-check"), **SYNC_VERSION_RESPONSES},
)
def stale_check(request: Request, payload: StaleCheckRequest) -> StaleCheckResponse:
    """Which offered live tracks did the fleet drop? Read-only.

    The spoke asks before it pushes, so it can refuse its own push and name
    every orphan at once instead of meeting the ``/push`` backstop one batch
    at a time. Same rule, same connection state: :mod:`apps.sync_hub.stale_copy`.
    """
    _require_same_wire(payload.wire_version, payload.schema_version)
    with _hub_conn(request) as conn:
        _require_credential(request, conn, payload.machine_id, "stale-check")
        _require_registered(conn, payload.machine_id)
        if payload.reseed:
            return StaleCheckResponse(orphans=[])
        verdict = stale_copy.classify(
            conn,
            [
                stale_copy.Candidate(
                    stable_id=candidate.stable_id,
                    origin_device_id=candidate.origin_device_id,
                )
                for candidate in payload.candidates
            ],
            payload.machine_id,
        )
    return StaleCheckResponse(orphans=list(verdict.orphans), unattributable=verdict.unattributable)


__all__ = ["push", "router", "stale_check"]
