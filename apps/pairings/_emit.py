"""Shared row formatting for ``apps.pairings.list`` output modes."""
from __future__ import annotations

from apps.shared.pairings.models import PairingEdge

COLUMNS: tuple[str, ...] = (
    "from_stable_id",
    "to_stable_id",
    "direction",
    "source",
    "notes",
    "confidence",
    "modified_at",
)


def format_confidence(confidence: float | None, *, precision: int) -> str:
    return "" if confidence is None else f"{confidence:.{precision}f}"


def edge_row_values(
    edge: PairingEdge,
    *,
    truncate_ids: bool,
    confidence_precision: int,
    modified_timespec: str | None,
) -> list[str]:
    from_id = edge.from_stable_id
    to_id = edge.to_stable_id
    if truncate_ids:
        from_id = from_id[:10] + "..."
        to_id = to_id[:10] + "..."
    modified = (
        edge.modified_at.isoformat(timespec=modified_timespec)
        if modified_timespec
        else edge.modified_at.isoformat()
    )
    return [
        from_id,
        to_id,
        edge.direction,
        edge.source,
        edge.notes or "",
        format_confidence(edge.confidence, precision=confidence_precision),
        modified,
    ]
