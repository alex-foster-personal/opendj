"""Contract surface for ``apps.rescue`` (M1, module contracts)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.rescue.contract import (
    CONTRACT_VERSION,
    RING_SIZE,
    RescueAppendReceipt,
    RescueService,
)
from apps.rescue.http import build_service


def _snapshot(captured_at_ms: int) -> dict[str, object]:
    return {
        "schema": 1,
        "captured_at_ms": captured_at_ms,
        "reason": "periodic",
        "app_posture": "gig",
        "decks": {},
    }


def test_build_service_satisfies_protocol_and_round_trips(tmp_path: Path) -> None:
    service = build_service()
    assert isinstance(service, RescueService)
    assert CONTRACT_VERSION == "rescue/1"

    assert service.read_latest(tmp_path) is None
    receipt = service.append_snapshot(tmp_path, _snapshot(1234))
    assert RescueAppendReceipt.model_validate(receipt).slot == 0
    assert service.read_latest(tmp_path) == _snapshot(1234)
    index = service.read_index(tmp_path)
    assert index.newest_slot == 0
    assert len(index.slots) == RING_SIZE


def test_receipt_rejects_extra_fields_and_is_frozen() -> None:
    with pytest.raises(ValidationError):
        RescueAppendReceipt(slot=0, captured_at_ms=1, bytes=2, extra=True)
    receipt = RescueAppendReceipt(slot=0, captured_at_ms=1, bytes=2)
    with pytest.raises(ValidationError):
        receipt.slot = 3  # type: ignore[misc]
