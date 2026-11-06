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


def _display_stable_id(stable_id: str, *, truncate: bool) -> str:
    if truncate:
        return stable_id[:10] + "..."
    return stable_id


def edge_row_values(edge: PairingEdge, opts: RowFormatOptions) -> list[str]:
    return [
        _display_stable_id(edge.from_stable_id, truncate=opts.truncate_ids),
        _display_stable_id(edge.to_stable_id, truncate=opts.truncate_ids),
        edge.direction,
        edge.source,
        edge.notes or "",
        format_confidence(edge.confidence, precision=opts.confidence_precision),
        format_modified_at(edge, timespec=opts.modified_timespec),
    ]
