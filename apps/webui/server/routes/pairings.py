"""Pairings CRUD endpoints (CAT-03 + CAT-05)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Query, Response, status

from apps.shared.events import publish

from ..backend import Pairing, StateBackend
from ..deps import get_read_state, get_write_state
from ..errors import precondition_required
from ..models import PairingCreate, PairingOut

router = APIRouter(prefix="/pairings", tags=["pairings"])


def _to_out(p: Pairing) -> PairingOut:
    return PairingOut(
        pairing_id=p.pairing_id, from_stable_id=p.from_stable_id,
        to_stable_id=p.to_stable_id, direction=p.direction,
        source=p.source, notes=p.notes, snapshot=p.snapshot,
        created_at=p.created_at, updated_at=p.updated_at,
    )


@router.get("", response_model=list[PairingOut])
def list_pairings(
    from_stable_id: str | None = Query(None),
    to_stable_id: str | None = Query(None),
    source: str | None = Query(None),
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> list[PairingOut]:
    items = backend.list_pairings(
        from_stable_id=from_stable_id, to_stable_id=to_stable_id, source=source,
    )
    return [_to_out(p) for p in items]


@router.post("", response_model=PairingOut, status_code=status.HTTP_201_CREATED)
def create_pairing(
    body: PairingCreate,
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> PairingOut:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = Pairing(
        pairing_id=str(uuid.uuid4()),
        from_stable_id=body.from_stable_id, to_stable_id=body.to_stable_id,
        direction=body.direction, source=body.source, notes=body.notes,
        snapshot=None if body.snapshot is None else body.snapshot.model_dump(),
        created_at=now, updated_at=now,
    )
    created = backend.create_pairing(p)
    publish("library.changed", {"kind": "pairings", "ids": [created.pairing_id]})
    return _to_out(created)


@router.delete("/{pairing_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_pairing(
    pairing_id: str,
    if_match: str | None = Header(None, alias="If-Match"),
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
):
    if not if_match:
        return precondition_required(
            "DELETE /pairings/{id} requires If-Match header"
        )
    backend.delete_pairing(pairing_id, expected_etag=if_match)
    publish("library.changed", {"kind": "pairings", "ids": [pairing_id]})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
