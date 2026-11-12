"""The hub side of the sync protocol, as a FastAPI router.

Mounted by the webui at ``/api/v1/sync/*``:

    POST /api/v1/sync/hello    register a spoke, learn the hub id and seq
    POST /api/v1/sync/enroll   join the fleet under a proved owner (ADR 12)
    POST /api/v1/sync/push     offer rows; the hub merges them under LWW
    GET  /api/v1/sync/pull     one chunk of the rows accepted after ``since_seq``
    GET  /api/v1/sync/status   hub identity, seq, fleet, row counts
    GET  /api/v1/sync/digest   per-table digests for the post-sync compare
    GET  /api/v1/sync/rows     paginated sync-eligible canonical rows for diff

Machines authenticate with a per-machine sync credential (plan X5), minted by
``/enroll`` (ADR 12) and checked on ``hello``, ``push``, ``pull``, ``status``
and ``digest`` by :mod:`apps.sync_hub.service_credentials`. The default
OBSERVE mode refuses nothing (it logs the verdict and ``hello`` reports it);
only ``MDT_SYNC_CREDENTIAL_MODE=enforce`` answers 401. Until then trust is
tailnet membership (ADR 04 c7), and reading this file as "the hub is
authenticated" would be exactly the error ``(private path)``
is about. Multi-user: ``(private path)``.

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

from fastapi import APIRouter

from apps.sync_hub.service_billing import router as billing_router
from apps.sync_hub.service_handshake import router as handshake_router
from apps.sync_hub.service_lyrics_asr_assets import router as lyrics_asr_assets_router
from apps.sync_hub.service_models import (
    DigestResponse,
    EnrollRequest,
    HashPendingResponse,
    HelloRequest,
    HelloResponse,
    MachineModel,
    PullResponse,
    PushRequest,
    PushResponse,
    RowModel,
    RowsResponse,
    StatusResponse,
    SyncRowSampleModel,
)
from apps.sync_hub.service_plugins import router as plugins_router
from apps.sync_hub.service_push import router as push_router
from apps.sync_hub.service_reads import router as reads_router
from apps.sync_hub.service_stem_assets import router as stem_assets_router
from apps.sync_hub.service_wiring import MAX_PULL_LIMIT

# Endpoints live in service_handshake, service_push and service_reads; shared
# helpers in service_wiring.

router = APIRouter(prefix="/sync", tags=["sync"])
router.include_router(stem_assets_router)
router.include_router(lyrics_asr_assets_router)
router.include_router(billing_router)
router.include_router(plugins_router)
router.include_router(handshake_router)
router.include_router(push_router)
router.include_router(reads_router)

__all__ = [
    "MAX_PULL_LIMIT",
    "DigestResponse",
    "EnrollRequest",
    "HashPendingResponse",
    "HelloRequest",
    "HelloResponse",
    "MachineModel",
    "PullResponse",
    "PushRequest",
    "PushResponse",
    "RowModel",
    "RowsResponse",
    "StatusResponse",
    "SyncRowSampleModel",
    "router",
]
