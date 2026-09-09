"""Turning stable_ids into queue candidates, against the real state layer.

Split out of :mod:`apps.analysis.queue` (600-line file ratchet) along the
seam that was already there: everything else in that module is pure queue
POLICY over rows the caller supplies, while this half reaches into
``tracks``, ``track_locations`` and the path map. Keeping the reach in one
place is also what lets a test drive the version-bump path with an explicit
list instead of a library.

-Claude
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import replace

from apps.shared import platform_paths
from apps.shared.state import locations as track_locations

from . import admission
from .backlog import _candidate_paths
from .queue import QueueError, TargetResolver

#-----------------------------------------------------------------------------
# target resolution
#-----------------------------------------------------------------------------

def candidates_from_state(
    conn: sqlite3.Connection,
    stable_ids: Sequence[str],
    *,
    lane: str,
    backend: str,
) -> list[admission.Candidate]:
    """Resolve stable_ids against the state layer into queue candidates.

    Duration comes from ``tracks.duration_ms``, which is what the library
    already knows; a row with no duration becomes a candidate with
    ``duration_s=None`` and is REFUSED by the admission rule with
    ``duration_unknown`` rather than admitted at an invented length.

    Paths go through the same resolution the backlog drain uses
    (``platform_paths.resolve_library_path`` over the legacy column plus
    ``track_locations``), so the queue and the drain cannot disagree about
    where a track's bytes are.
    """
    if not stable_ids:
        return []
    wanted = list(stable_ids)
    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"SELECT stable_id, file_path, duration_ms FROM tracks "
        f"WHERE stable_id IN ({placeholders})",
        wanted,
    ).fetchall()
    found = {row[0] for row in rows}
    missing = [sid for sid in wanted if sid not in found]
    if missing:
        raise QueueError(
            f"{len(missing)} stable_id(s) are not in tracks: {missing[:5]}"
        )
    path_map = platform_paths.load_path_map()
    locations = track_locations.list_location_paths(conn, wanted)
    out: list[admission.Candidate] = []
    for stable_id, file_path, duration_ms in rows:
        resolved: str | None = None
        for candidate in _candidate_paths(
            file_path, locations.get(stable_id, ())
        ):
            mapped = platform_paths.resolve_library_path(
                candidate, path_map=path_map
            )
            if mapped.resolved is not None:
                resolved = str(mapped.resolved)
                break
        if resolved is None:
            # No local bytes on THIS machine. Offered anyway, with no
            # duration, so the admission rule refuses it by name instead of
            # the queue dropping it silently.
            out.append(
                admission.Candidate(
                    stable_id=stable_id,
                    lane=lane,
                    backend=backend,
                    file_path="",
                    duration_s=None,
                )
            )
            continue
        out.append(
            admission.Candidate(
                stable_id=stable_id,
                lane=lane,
                backend=backend,
                file_path=resolved,
                duration_s=(duration_ms / 1000.0) if duration_ms else None,
            )
        )
    return out


def with_lane(
    candidate: admission.Candidate, *, lane: str, backend: str
) -> admission.Candidate:
    """Same track, different lane. Used when one enqueue covers two lanes."""
    return replace(candidate, lane=lane, backend=backend)


__all__ = [
    "TargetResolver",
    "candidates_from_state",
    "with_lane",
]
