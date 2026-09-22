"""Process-local, per-track CloudSync asset transfer progress.

The ledger is deliberately ephemeral: it describes only operations currently
executing in this engine process. Durable cloud presence stays in
``track_locations``. A generation token prevents an older operation's finalizer
from clearing a newer transfer for the same track.
"""
from __future__ import annotations

import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

TransferDirection = Literal["upload", "download"]


@dataclass(frozen=True)
class CloudTransfer:
    direction: TransferDirection
    bytes_transferred: int
    bytes_total: int | None


@dataclass(frozen=True)
class _Entry:
    token: str
    transfer: CloudTransfer


_LOCK = threading.Lock()
_ACTIVE: dict[str, _Entry] = {}


def _validate_bytes(bytes_transferred: int, bytes_total: int | None) -> None:
    if isinstance(bytes_transferred, bool) or bytes_transferred < 0:
        raise ValueError("bytes_transferred must be a non-negative integer")
    if bytes_total is not None:
        if isinstance(bytes_total, bool) or bytes_total < 0:
            raise ValueError("bytes_total must be null or a non-negative integer")
        if bytes_transferred > bytes_total:
            raise ValueError("bytes_transferred must not exceed bytes_total")


def begin_transfer(
    stable_id: str,
    direction: TransferDirection,
    *,
    bytes_total: int | None,
) -> str:
    if not stable_id:
        raise ValueError("stable_id must not be empty")
    if direction not in ("upload", "download"):
        raise ValueError("direction must be upload or download")
    _validate_bytes(0, bytes_total)
    token = uuid.uuid4().hex
    with _LOCK:
        _ACTIVE[stable_id] = _Entry(
            token=token,
            transfer=CloudTransfer(direction, 0, bytes_total),
        )
    return token


def update_transfer(stable_id: str, token: str, bytes_transferred: int) -> bool:
    with _LOCK:
        entry = _ACTIVE.get(stable_id)
        if entry is None or entry.token != token:
            return False
        _validate_bytes(bytes_transferred, entry.transfer.bytes_total)
        if bytes_transferred < entry.transfer.bytes_transferred:
            raise ValueError("bytes_transferred must not regress")
        _ACTIVE[stable_id] = _Entry(
            token=token,
            transfer=CloudTransfer(
                entry.transfer.direction,
                bytes_transferred,
                entry.transfer.bytes_total,
            ),
        )
        return True


def clear_transfer(stable_id: str, token: str) -> bool:
    with _LOCK:
        entry = _ACTIVE.get(stable_id)
        if entry is None or entry.token != token:
            return False
        del _ACTIVE[stable_id]
        return True


def transfer_for(stable_id: str) -> CloudTransfer | None:
    with _LOCK:
        entry = _ACTIVE.get(stable_id)
        return entry.transfer if entry is not None else None


def transfers_for(stable_ids: Sequence[str]) -> dict[str, CloudTransfer]:
    wanted = set(stable_ids)
    with _LOCK:
        return {
            stable_id: entry.transfer
            for stable_id, entry in _ACTIVE.items()
            if stable_id in wanted
        }


__all__ = [
    "CloudTransfer",
    "TransferDirection",
    "begin_transfer",
    "clear_transfer",
    "transfer_for",
    "transfers_for",
    "update_transfer",
]
