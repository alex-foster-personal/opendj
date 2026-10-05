"""Agent-readable UI mirror state.

The performance page owns the audio engine, so it pushes its rendered state to
this small engine-side store. A missing push is a closed page, never an empty
mirror that could be mistaken for an idle four-deck screen.

Agents compute staleness from ``received_at``; ``meter.age_ms`` is page-relative
and freezes with the page.
"""
from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from apps.webui.server.headphone_reports import client_id_of, headphone_reports

router = APIRouter(prefix="/state", tags=["agent-state"])
_LOG = logging.getLogger(__name__)

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
    # Bug #31: taken with Take control. An operator's explicit choice is never handed
    # back to a playing tab by the audible rule (the old leader is still playing for
    # the moment it takes to go silent). It lasts while the same holder renews it.
    operator_claimed: bool = False


def _live_lease(request: Request) -> MirrorLease | None:
    lease = getattr(request.app.state, "ui_mirror_lease", None)
    if lease is None:
        return None
    if not isinstance(lease, MirrorLease):
        raise TypeError("app.state.ui_mirror_lease must be a MirrorLease")
    if lease.expires_monotonic <= monotonic():
        return None
    return lease


def _published_at(document: dict[str, Any] | None) -> datetime | None:
    value = None if document is None else document.get("published_at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _refuse(body: dict[str, Any], reason: str, extra: dict[str, Any]) -> JSONResponse:
    _LOG.warning(
        "ui-mirror PUT refused: reason=%s client_id=%s published_at=%s %s",
        reason,
        body.get("client_id"),
        body.get("published_at"),
        extra,
    )
    return JSONResponse(status_code=409, content={"accepted": False, "reason": reason, **extra})


def _audible(document: dict[str, Any]) -> bool:
    """Any deck in this mirror document is playing."""
    decks = document.get("decks")
    if not isinstance(decks, dict):
        return False
    return any(isinstance(deck, dict) and deck.get("playing") is True for deck in decks.values())


def _yieldable(document: dict[str, Any]) -> bool:
    """A hidden tab with nothing playing: it gives the lease to any visible claimant."""
    tab = document.get("tab")
    return isinstance(tab, dict) and tab.get("visible") is False and not _audible(document)


def _holder_document(request: Request, lease: MirrorLease) -> dict[str, Any] | None:
    """The holder's own latest snapshot, or None when the stored one is someone else's."""
    stored = _mirror_store(request)
    if stored is None or stored.get("client_id") != lease.holder:
        return None
    return stored


def _lease_body(request: Request, lease: MirrorLease | None) -> dict[str, Any]:
    ttl_ms = int(LEASE_TTL_S * 1000)
    if lease is None:
        return {
            "held": False,
            "holder": None,
            "expires_in_ms": None,
            "ttl_ms": ttl_ms,
            "holder_playing": None,
            "holder_yieldable": None,
            "holder_operator_claimed": None,
        }
    holder_doc = _holder_document(request, lease)
    return {
        "held": True,
        "holder": lease.holder,
        "expires_in_ms": max(0, int((lease.expires_monotonic - monotonic()) * 1000)),
        "ttl_ms": ttl_ms,
        "holder_playing": None if holder_doc is None else _audible(holder_doc),
        "holder_yieldable": None if holder_doc is None else _yieldable(holder_doc),
        "holder_operator_claimed": lease.operator_claimed,
    }


def _handover_reason(request: Request, lease: MirrorLease, body: dict[str, Any]) -> str | None:
    """AGENT-18: why a non-holder may take the lease without a takeover, or None.

    The engine prefers an audible writer: a holder that is silent loses the
    lease to a claimant that is playing, and a hidden, silent holder loses it
    to any visible claimant. Mon 5 Oct 2026: an idle backgrounded Chrome tab
    held the lease while an agent pane played the set.
    """
    holder_doc = _holder_document(request, lease)
    if holder_doc is None:
        return None
    claimant_tab = body.get("tab")
    claimant_visible = not (isinstance(claimant_tab, dict) and claimant_tab.get("visible") is False)
    if _audible(body) and not _audible(holder_doc) and not lease.operator_claimed:
        return "audible_over_silent"
    if _yieldable(holder_doc) and claimant_visible:
        return "visible_over_hidden_idle"
    return None


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
    # AGENT-18: a snapshot older than the live one is a stale or out-of-order
    # write (a dead page's queued PUT, a slow fetch overtaken by a newer one).
    # It never replaces fresher state. Once the stored one is older than the
    # lease TTL, any writer may replace it, so browser clock skew cannot wedge it.
    current_doc = _mirror_store(request)
    new_at, old_at = _published_at(body), _published_at(current_doc)
    stored_age_s = monotonic() - getattr(request.app.state, "ui_mirror_received_monotonic", float("-inf"))
    # Compared within ONE writer (its own reordered PUTs), or for unleased writers.
    # Between two leased tabs the lease decides; their clocks may disagree (Mon 5
    # Oct 2026: a second browser's takeover was refused as "stale" by 2 s of skew).
    same_writer = current_doc is not None and current_doc.get("client_id") == body.get("client_id")
    if (
        takeover != "1"
        and (same_writer or lease_id is None)
        and new_at is not None
        and old_at is not None
        and new_at < old_at
        and stored_age_s < LEASE_TTL_S
    ):
        return _refuse(
            body,
            "stale_snapshot",
            {"stored_published_at": old_at.isoformat()},
        )
    if lease_id is not None:
        if lease_id == "" or body.get("client_id", lease_id) != lease_id:
            return JSONResponse(
                status_code=422,
                content={"detail": "x-opendj-lease must equal the body's client_id"},
            )
        current = _live_lease(request)
        if current is not None and current.holder != lease_id and takeover != "1":
            reason = _handover_reason(request, current, body)
            if reason is None:
                return _refuse(body, "lease_held", _lease_body(request, current))
            _LOG.warning(
                "ui-mirror lease handover: reason=%s from=%s to=%s",
                reason,
                current.holder,
                lease_id,
            )
        elif current is not None and current.holder != lease_id:
            _LOG.warning("ui-mirror lease takeover: from=%s to=%s", current.holder, lease_id)
        if takeover == "1":
            operator_claimed = True
        elif current is not None and current.holder == lease_id:
            operator_claimed = current.operator_claimed
        else:
            operator_claimed = False
        request.app.state.ui_mirror_lease = MirrorLease(
            lease_id, monotonic() + LEASE_TTL_S, operator_claimed=operator_claimed
        )
    stored = deepcopy(body)
    stored["received_at"] = _received_at()
    request.app.state.ui_mirror = stored
    request.app.state.ui_mirror_received_monotonic = monotonic()
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
    return _lease_body(request, _live_lease(request))


@router.get("/ui-mirror", response_model=None)
async def get_ui_mirror(request: Request) -> dict[str, Any] | JSONResponse:
    """Return the latest page mirror, or fail explicitly when no page is open."""
    mirror = _mirror_store(request)
    if mirror is None:
        return JSONResponse(status_code=409, content={"client_open": False})
    return deepcopy(mirror)
