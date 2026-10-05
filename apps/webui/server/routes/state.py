"""Agent-readable UI mirror state.

The performance page owns the audio engine, so it pushes its rendered state to
this small engine-side store. A missing push is a closed page, never an empty
mirror that could be mistaken for an idle four-deck screen.

Agents compute staleness from ``received_at``; ``meter.age_ms`` is page-relative
and freezes with the page.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from apps.webui.server.headphone_reports import client_id_of, headphone_reports

router = APIRouter(prefix="/state", tags=["agent-state"])

# AGENT-18: one writer per engine. A page that sends the lease header claims the
# mirror for LEASE_TTL_S; a different leased writer is refused with 409 until the
# holder lapses, closes, or is taken over. PUTs without the header (older pages,
# agents, tests) are unaffected.
LEASE_HEADER = "x-opendj-lease"
LEASE_TAKEOVER_HEADER = "x-opendj-lease-takeover"
LEASE_TTL_S = 10.0


@dataclass(frozen=True)
class MirrorLease:
    holder: str
    expires_monotonic: float


def _live_lease(request: Request) -> MirrorLease | None:
    lease = getattr(request.app.state, "ui_mirror_lease", None)
    if lease is None:
        return None
    if not isinstance(lease, MirrorLease):
        raise TypeError("app.state.ui_mirror_lease must be a MirrorLease")
    if lease.expires_monotonic <= monotonic():
        return None
    return lease


def _lease_body(lease: MirrorLease | None) -> dict[str, Any]:
    if lease is None:
        return {"held": False, "holder": None, "expires_in_ms": None, "ttl_ms": int(LEASE_TTL_S * 1000)}
    return {
        "held": True,
        "holder": lease.holder,
        "expires_in_ms": max(0, int((lease.expires_monotonic - monotonic()) * 1000)),
        "ttl_ms": int(LEASE_TTL_S * 1000),
    }


def _received_at() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _mirror_store(request: Request) -> dict[str, Any] | None:
    mirror = getattr(request.app.state, "ui_mirror", None)
    if mirror is None:
        return None
    if not isinstance(mirror, dict):
        raise TypeError("app.state.ui_mirror must be a JSON object")
    return mirror


@router.put("/ui-mirror", status_code=202, response_model=None)
async def publish_ui_mirror(
    request: Request,
    body: dict[str, Any],
    lease_id: str | None = Header(
        default=None,
        alias=LEASE_HEADER,
        description="AGENT-18: the publishing page's client id, claiming the mirror lease.",
    ),
    takeover: str | None = Header(
        default=None,
        alias=LEASE_TAKEOVER_HEADER,
        description="AGENT-18: '1' when the operator pressed Take control on this page.",
    ),
) -> dict[str, bool] | JSONResponse:
    """Replace the page's current screen document with strict JSON input."""
    if lease_id is not None:
        if lease_id == "" or body.get("client_id", lease_id) != lease_id:
            return JSONResponse(
                status_code=422,
                content={"detail": "x-opendj-lease must equal the body's client_id"},
            )
        current = _live_lease(request)
        if current is not None and current.holder != lease_id and takeover != "1":
            return JSONResponse(
                status_code=409,
                content={"accepted": False, "reason": "lease_held", **_lease_body(current)},
            )
        request.app.state.ui_mirror_lease = MirrorLease(lease_id, monotonic() + LEASE_TTL_S)
    stored = deepcopy(body)
    stored["received_at"] = _received_at()
    request.app.state.ui_mirror = stored
    # CUEOUT-18: the mirror stays last-writer-wins; the headphone state is
    # also kept per reporting client, so one tab cannot overwrite another's.
    mixer = body.get("mixer")
    headphones = mixer.get("headphones") if isinstance(mixer, dict) else None
    if isinstance(headphones, dict):
        headphone_reports(request.app).record(client_id_of(body), headphones)
    return {"accepted": True}


@router.delete("/ui-mirror", status_code=204)
async def close_ui_mirror(
    request: Request,
    client_id: str | None = Header(
        default=None,
        alias="x-opendj-client-id",
        description=(
            "The closing page's client id (CUEOUT-18). Its headphone report is "
            "forgotten; with no id, every client's report is."
        ),
    ),
) -> None:
    """Mark the performance page closed during its unmount lifecycle."""
    headphone_reports(request.app).forget(client_id)
    current = _live_lease(request)
    if current is not None and client_id is not None and client_id != current.holder:
        # AGENT-18: a non-leader closing must not blank the leader's live mirror.
        return
    request.app.state.ui_mirror_lease = None
    request.app.state.ui_mirror = None


@router.get("/ui-mirror/lease")
async def get_ui_mirror_lease(request: Request) -> dict[str, Any]:
    """AGENT-18: which page holds the mirror lease (always 200; ``held`` false when none)."""
    return _lease_body(_live_lease(request))


@router.get("/ui-mirror", response_model=None)
async def get_ui_mirror(request: Request) -> dict[str, Any] | JSONResponse:
    """Return the latest page mirror, or fail explicitly when no page is open."""
    mirror = _mirror_store(request)
    if mirror is None:
        return JSONResponse(status_code=409, content={"client_open": False})
    return deepcopy(mirror)
