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

import logging
import sqlite3
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from apps.shared import platform_paths
from apps.shared.ffmpeg import FfmpegUnavailable, probe_duration_s, resolve_ffmpeg
from apps.shared.state import locations as track_locations

from . import admission
from .backlog import _candidate_paths
from .queue import QueueError, TargetResolver

# Keep IN (...) under SQLite's default 999 bind cap (see apps.shared.state.queries).
_SQL_CHUNK: int = 500
#: Concurrent ffmpeg header probes for rows with no stored duration.
_PROBE_WORKERS: int = 8

log = logging.getLogger("apps.analysis.queue_targets")

#-----------------------------------------------------------------------------
# target resolution
#-----------------------------------------------------------------------------

def _tracks_by_stable_id(
    conn: sqlite3.Connection, stable_ids: Sequence[str]
) -> list[tuple[str, str | None, int | None]]:
    """Live track rows for ``stable_ids``, chunked for SQLite bind limits."""
    wanted = list(dict.fromkeys(stable_ids))
    if not wanted:
        return []
    rows: list[tuple[str, str | None, int | None]] = []
    for offset in range(0, len(wanted), _SQL_CHUNK):
        chunk = wanted[offset : offset + _SQL_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows.extend(
            conn.execute(
                f"SELECT stable_id, file_path, duration_ms FROM tracks "
                f"WHERE stable_id IN ({placeholders}) AND deleted_at IS NULL",
                chunk,
            ).fetchall()
        )
    return rows


def _measured_durations(paths: dict[str, str]) -> dict[str, float | None]:
    """ffmpeg's stated length for each track whose row stores none.

    A folder import writes ``duration_ms = NULL`` whenever the tag reader
    states no length (payloads built before tinytag replaced mutagen, Thu 1
    Oct 2026, did so for EVERY track), and admission would refuse those
    tracks. ffmpeg is the decoder every lane already
    requires, so its header is the one honest source here. Without ffmpeg no
    lane can run anyway: every such track stays ``None`` and is refused by
    name, and the cause is logged once rather than per track.
    """
    if not paths:
        return {}
    try:
        resolve_ffmpeg()
    except FfmpegUnavailable as exc:
        log.warning("cannot measure %d track duration(s): %s", len(paths), exc)
        return dict.fromkeys(paths)
    with ThreadPoolExecutor(max_workers=_PROBE_WORKERS) as pool:
        lengths = pool.map(lambda path: probe_duration_s(Path(path)), paths.values())
        return dict(zip(paths, lengths, strict=True))


def _resolve_local_path(
    file_path: str | None,
    locations: Sequence[str],
    path_map: platform_paths.PathMap,
) -> str | None:
    """The first of a track's recorded paths that exists on THIS machine."""
    for candidate in _candidate_paths(file_path, locations):
        mapped = platform_paths.resolve_library_path(candidate, path_map=path_map)
        if mapped.resolved is not None:
            return str(mapped.resolved)
    return None


def _admission_duration_s(
    resolved: str | None, duration_ms: int | None, measured_s: float | None
) -> float | None:
    """The stored length wins; else ffmpeg's; a track with no local bytes has none."""
    if resolved is None:
        return None
    if duration_ms:
        return duration_ms / 1000.0
    return measured_s


def candidates_from_state(
    conn: sqlite3.Connection,
    stable_ids: Sequence[str],
    *,
    lane: str,
    backend: str,
) -> list[admission.Candidate]:
    """Resolve stable_ids against the state layer into queue candidates.

    Duration comes from ``tracks.duration_ms``, which is what the library
    already knows. A row with none is measured from the resolved file's
    header by ffmpeg (:func:`_measured_durations`); one ffmpeg cannot state a
    length for becomes a candidate with ``duration_s=None`` and is REFUSED by
    the admission rule with ``duration_unknown`` rather than admitted at an
    invented length.

    Paths go through the same resolution the backlog drain uses
    (``platform_paths.resolve_library_path`` over the legacy column plus
    ``track_locations``), so the queue and the drain cannot disagree about
    where a track's bytes are.

    The read filters ``deleted_at``: nothing hard-deletes a synced row here,
    so a removed track is a tombstone that is still selectable. Asking for
    one raises ``QueueError`` alongside the ids that never existed, because
    both mean the same thing to the caller -- the library has no live track
    under that id -- and a silent drop would spend a worker on a file the
    user deleted.
    """
    if not stable_ids:
        return []
    wanted = list(stable_ids)
    rows = _tracks_by_stable_id(conn, wanted)
    found = {row[0] for row in rows}
    missing = [sid for sid in wanted if sid not in found]
    if missing:
        raise QueueError(
            f"{len(missing)} stable_id(s) are not a live track "
            f"(unknown or deleted): {missing[:5]}"
        )
    path_map = platform_paths.load_path_map()
    locations = track_locations.list_location_paths(conn, wanted)
    resolved_rows = [
        (sid, _resolve_local_path(file_path, locations.get(sid, ()), path_map), ms)
        for sid, file_path, ms in rows
    ]
    measured = _measured_durations(
        {sid: path for sid, path, ms in resolved_rows if path is not None and not ms}
    )
    # A track with no local bytes on THIS machine is offered anyway, with no
    # duration, so the admission rule refuses it by name instead of the queue
    # dropping it silently.
    return [
        admission.Candidate(
            stable_id=sid,
            lane=lane,
            backend=backend,
            file_path=resolved or "",
            duration_s=_admission_duration_s(resolved, ms, measured.get(sid)),
        )
        for sid, resolved, ms in resolved_rows
    ]


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
