"""Pairings CRUD endpoints (CAT-03 + CAT-05)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Response, status

from ..backend import Pairing, StateBackend
from ..deps import get_read_state, get_write_state
from ..errors import precondition_required
from ..models import PairingCreate, PairingOut

router = APIRouter(prefix="/pairings", tags=["pairings"])


def _to_out(p: Pairing) -> PairingOut:
    return PairingOut(
        pairing_id=p.pairing_id, from_stable_id=p.from_stable_id,
        to_stable_id=p.to_stable_id, direction=p.direction,
        source=p.source, notes=p.notes,
        created_at=p.created_at, updated_at=p.updated_at,
    )


@router.get("", response_model=list[PairingOut])
def list_pairings(
    from_stable_id: Optional[str] = Query(None),
    to_stable_id: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    backend: StateBackend = Depends(get_read_state),
) -> list[PairingOut]:
    items = backend.list_pairings(
        from_stable_id=from_stable_id, to_stable_id=to_stable_id, source=source,
    )
    return [_to_out(p) for p in items]


@router.post("", response_model=PairingOut, status_code=status.HTTP_201_CREATED)
def create_pairing(
    body: PairingCreate,
    backend: StateBackend = Depends(get_write_state),
) -> PairingOut:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = Pairing(
        pairing_id=str(uuid.uuid4()),
        from_stable_id=body.from_stable_id, to_stable_id=body.to_stable_id,
        direction=body.direction, source=body.source, notes=body.notes,
        created_at=now, updated_at=now,
    )
    return _to_out(backend.create_pairing(p))


@router.delete("/{pairing_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_pairing(
    pairing_id: str,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    backend: StateBackend = Depends(get_write_state),
):
    if not if_match:
        return precondition_required(
            "DELETE /pairings/{id} requires If-Match header"
        )
    backend.delete_pairing(pairing_id, expected_etag=if_match)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
