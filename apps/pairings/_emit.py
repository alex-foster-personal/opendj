"""Shared row formatting for ``apps.pairings.list`` output modes."""
from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class RowFormatOptions:
    truncate_ids: bool
    confidence_precision: int
    modified_timespec: str | None


CSV_ROW_OPTS = RowFormatOptions(False, 3, None)
TABLE_FALLBACK_ROW_OPTS = RowFormatOptions(True, 2, "seconds")
TABLE_RICH_ROW_OPTS = RowFormatOptions(False, 2, "seconds")


def format_confidence(confidence: float | None, *, precision: int) -> str:
    return "" if confidence is None else f"{confidence:.{precision}f}"


def format_modified_at(edge: PairingEdge, *, timespec: str | None) -> str:
    if timespec:
        return edge.modified_at.isoformat(timespec=timespec)
    return edge.modified_at.isoformat()


def edge_row_values(edge: PairingEdge, opts: RowFormatOptions) -> list[str]:
    from_id = edge.from_stable_id
    to_id = edge.to_stable_id
    if opts.truncate_ids:
        from_id = from_id[:10] + "..."
        to_id = to_id[:10] + "..."
    return [
        from_id,
        to_id,
        edge.direction,
        edge.source,
        edge.notes or "",
        format_confidence(edge.confidence, precision=opts.confidence_precision),
        format_modified_at(edge, timespec=opts.modified_timespec),
    ]
