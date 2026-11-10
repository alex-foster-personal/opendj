"""Summed duration for a list of tracks (LIBUX list footer parity).

One aggregate shared by ``POST /api/v1/tracks/list-totals``, ``opendj track
list-totals``, MCP ``tracks_list_totals``, and the library footer TypeScript
mirror (``apps/webui/frontend/src/lib/rb/list-totals.ts``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class ListTotals:
    count: int
    duration_ms: int
    unknown: int

    def to_dict(self) -> dict[str, int]:
        return {"count": self.count, "duration_ms": self.duration_ms, "unknown": self.unknown}


def summarize_durations_ms(durations_ms: Iterable[int | None]) -> ListTotals:
    count = 0
    total = 0
    unknown = 0
    for d in durations_ms:
        count += 1
        if d is None or not isinstance(d, (int, float)) or d < 0:
            unknown += 1
            continue
        total += int(d)
    return ListTotals(count=count, duration_ms=total, unknown=unknown)


def format_duration_hms(ms: int) -> str:
    total = max(0, int(round(ms / 1000)))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    ss = f"{s:02d}"
    if h > 0:
        return f"{h}:{m:02d}:{ss}"
    return f"{m}:{ss}"


def format_list_totals_line(totals: ListTotals) -> str:
    label = "track" if totals.count == 1 else "tracks"
    time = format_duration_hms(totals.duration_ms)
    base = f"{totals.count} {label}, {time}"
    if totals.unknown > 0:
        base += f" (+{totals.unknown} unknown)"
    return base


def format_selection_suffix(totals: ListTotals) -> str:
    if totals.count < 2:
        return ""
    label = "track" if totals.count == 1 else "tracks"
    time = format_duration_hms(totals.duration_ms)
    unk = f" (+{totals.unknown} unknown)" if totals.unknown > 0 else ""
    return f"selected: {totals.count} {label}, {time}{unk}"


def format_library_footer(list_totals: ListTotals, selection_totals: ListTotals | None) -> str:
    line = format_list_totals_line(list_totals)
    if selection_totals is None or selection_totals.count < 2:
        return line
    return f"{line} · {format_selection_suffix(selection_totals)}"


__all__ = [
    "ListTotals",
    "format_duration_hms",
    "format_library_footer",
    "format_list_totals_line",
    "format_selection_suffix",
    "summarize_durations_ms",
]
