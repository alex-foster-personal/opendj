"""Shared bounds for sync hub machine registry fields on the wire."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.sync_hub.protocol import MachineRow

MACHINE_NAME_MAX_LENGTH: int = 256
MACHINE_ID_MAX_LENGTH: int = 256


class MachineWireLimitError(Exception):
    """Raised when machine_id or name exceeds the wire ceiling."""


def validate_machine_wire_fields(machine_id: str, name: str) -> None:
    if len(machine_id) > MACHINE_ID_MAX_LENGTH:
        raise MachineWireLimitError(
            f"machine_id exceeds {MACHINE_ID_MAX_LENGTH} characters"
        )
    if len(name) > MACHINE_NAME_MAX_LENGTH:
        raise MachineWireLimitError(
            f"name exceeds {MACHINE_NAME_MAX_LENGTH} characters"
        )


def clamp_machine_row_for_wire(row: MachineRow) -> MachineRow:
    """Return a copy safe to embed in hello/pull/status JSON."""
    from apps.sync_hub.protocol import MachineRow

    return MachineRow(
        machine_id=row.machine_id[:MACHINE_ID_MAX_LENGTH],
        name=row.name[:MACHINE_NAME_MAX_LENGTH],
        platform=row.platform,
        is_hub=row.is_hub,
        data_root=row.data_root,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
    )


__all__ = [
    "MACHINE_ID_MAX_LENGTH",
    "MACHINE_NAME_MAX_LENGTH",
    "MachineWireLimitError",
    "clamp_machine_row_for_wire",
    "validate_machine_wire_fields",
]
