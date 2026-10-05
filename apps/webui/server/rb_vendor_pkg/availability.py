"""Budgeted disk-truth probing for listing rows (issue #1037, PERF-RB-01).

Answers "is this library path's audio on disk" from, in order: the in-process
L1 cache (``config._FILE_EXISTS_CACHE``), the persisted ``path_availability``
index (:mod:`.path_index`), and at most a per-request budget of real stats.
Whatever the budget cannot cover comes back ``AVAILABILITY_PENDING`` and is
handed to the background refresher, never guessed in either direction.

Two invariants the call sites rely on:

* ONE budget per request. :class:`ProbeBudget` is created once and passed to
  every probe that request makes, so primary paths and ``track_locations``
  alternates draw from the same allowance (16 stats for row hydration, 0 for
  the playlist tree), never 16 each.
* An API whose return type cannot say "pending" runs a FULL scan.
  :func:`bulk_file_exists`, :func:`bulk_file_size` and the boolean
  :func:`bulk_availability` in ``track_rows`` answer every path, because a
  bool that silently maps pending to False turns an unprobed file into a
  broken link (the reconcile scan would then offer to "repair" it).
"""
from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from apps.adapters.rekordbox import config
from apps.adapters.rekordbox.errors import _open_ro
from apps.adapters.rekordbox.paths import is_streaming_path, resolve_asset_path
from apps.shared import fs_residency, remote_status
from apps.shared.state import db as state_db
from apps.shared.state import locations as track_locations
from apps.shared.state.locations import ID_BIND_BATCH

from .. import path_availability_refresh
from . import path_index

FileAvailabilityStatus = Literal[
    "present",
    "absent",
    "AVAILABILITY_PENDING",
    "streaming",
    "awaiting_volume",
]
PENDING: FileAvailabilityStatus = "AVAILABILITY_PENDING"

PROBE_BUDGET_TREE_SUMMARY: int = 0
PROBE_BUDGET_ROW_HYDRATION: int = 16


class AvailabilityProbeMode(Enum):
    """Per-request stat budget selector for listing hydration."""

    TREE_SUMMARY = "tree_summary"
    ROW_HYDRATION = "row_hydration"
    FULL_SCAN = "full_scan"

    @property
    def budget(self) -> int | None:
        """Stats one request may spend; None means every path is answered."""
        if self is AvailabilityProbeMode.TREE_SUMMARY:
            return PROBE_BUDGET_TREE_SUMMARY
        if self is AvailabilityProbeMode.ROW_HYDRATION:
            return PROBE_BUDGET_ROW_HYDRATION
        return None


class ProbeBudget:
    """The stats one request may still spend, shared by all its probes."""

    def __init__(self, mode: AvailabilityProbeMode) -> None:
        self.mode = mode
        self.remaining = mode.budget
        self.spent = 0

    @property
    def serves_stale(self) -> bool:
        """Tree summaries serve a stale index answer (and refresh it in the
        background) rather than turning it pending: they may not stat at all,
        and a count that lags by one refresh beats a count that drops rows."""
        return self.mode is AvailabilityProbeMode.TREE_SUMMARY

    def take(self) -> bool:
        if self.remaining is not None:
            if self.remaining <= 0:
                return False
            self.remaining -= 1
        self.spent += 1
        return True


@dataclass(frozen=True)
class PathProbeResult:
    status: FileAvailabilityStatus
    materialised_size: int | None = None


def _size_result(size: int | None) -> PathProbeResult:
    return PathProbeResult("present" if size is not None else "absent", size)


_PENDING_RESULT = PathProbeResult(PENDING)


def status_to_file_exists(status: FileAvailabilityStatus) -> bool | None:
    """Wire ``file_exists``: null exactly when availability is pending."""
    if status == PENDING:
        return None
    return status == "present"


def _stat_size(resolved: Path | None) -> int | None:
    """Materialised st_size, or None when missing / not a file / dataless stub."""
    if resolved is None:
        return None
    return fs_residency.materialised_size(resolved)


def _l1_hits(wanted: Sequence[str], now: float) -> dict[str, int | None]:
    with config._FILE_EXISTS_LOCK:
        hits = {path: config._FILE_EXISTS_CACHE.get(path) for path in wanted}
    return {
        path: hit[1]
        for path, hit in hits.items()
        if hit is not None and now - hit[0] < config.FILE_EXISTS_TTL_S
    }


def _load_index(
    namespace: str, paths: Sequence[str]
) -> dict[str, path_index.IndexEntry | None]:
    if not paths or not config.STATE_DB.exists():
        return {}
    conn = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        return path_index.bulk_lookup(conn, namespace, paths)
    finally:
        conn.close()


def _record_stats(namespace: str, sizes: Mapping[str, int | None], now: float) -> None:
    """Cache this request's stats in L1, and persist them off the request thread.

    A read request never opens a write connection while the server runs
    (STATE-18): the running refresher persists the rows, so a writer holding
    the state.db lock cannot turn ``GET /api/v1/tracks`` into a 503. With no
    refresher running (CLI, script, no-lifespan test) the rows are written
    here, as before, because nothing else would write them.
    """
    if not sizes:
        return
    with config._FILE_EXISTS_LOCK:
        for path, size in sizes.items():
            config._FILE_EXISTS_CACHE[path] = (now, size)
    rows = list(sizes.items())
    if path_availability_refresh.record(namespace, rows):
        return
    if config.STATE_DB.exists():
        conn = state_db.open_rw(config.STATE_DB)
        try:
            path_index.upsert_rows(conn, namespace, rows)
            conn.commit()
        finally:
            conn.close()


def _triage(
    paths: Sequence[str],
    index: Mapping[str, path_index.IndexEntry | None],
    budget: ProbeBudget,
    out: dict[str, PathProbeResult],
) -> tuple[list[str], list[str]]:
    """Answer fresh index hits into ``out``; split the rest into the paths
    this request stats now and the ones handed to the background refresher
    (served stale for a tree summary, else pending)."""
    stat_now: list[str] = []
    background: list[str] = []
    for path in paths:
        entry = index.get(path)
        if entry is not None and not entry.stale:
            out[path] = _size_result(entry.materialised_size)
        elif budget.take():
            stat_now.append(path)
        else:
            background.append(path)
            out[path] = (
                _size_result(entry.materialised_size)
                if entry is not None and budget.serves_stale
                else _PENDING_RESULT
            )
    return stat_now, background


def bulk_probe_paths(
    paths: Iterable[str],
    *,
    probe_mode: AvailabilityProbeMode = AvailabilityProbeMode.ROW_HYDRATION,
    budget: ProbeBudget | None = None,
    trust_index: bool = True,
) -> dict[str, PathProbeResult]:
    """Disk truth or ``AVAILABILITY_PENDING`` for each unique local path.

    ``budget`` is the request's shared allowance; without one, a fresh
    budget for ``probe_mode`` is used (one call = one request).

    ``trust_index`` is false for tracks with no ``track_availability`` row.
    A copied ``path_availability`` index can mark another machine's path
    present with no stat here; those paths spend a budgeted stat, or come
    back pending. They are never reported present from the index alone.
    """
    wanted = list(dict.fromkeys(p for p in paths if p))
    if not wanted:
        return {}
    budget = budget if budget is not None else ProbeBudget(probe_mode)
    now = time.monotonic()
    out = {path: _size_result(size) for path, size in _l1_hits(wanted, now).items()}
    rest = [path for path in wanted if path not in out]
    if not rest:
        return out
    namespace = path_index.resolver_namespace(config.DATA_DIR)
    index = _load_index(namespace, rest) if trust_index else {}
    stat_now, background = _triage(rest, index, budget, out)
    sizes = {path: _stat_size(resolve_asset_path(path).resolved) for path in stat_now}
    out.update((path, _size_result(size)) for path, size in sizes.items())
    _record_stats(namespace, sizes, now)
    if background:
        path_availability_refresh.schedule(background)
    return out


def bulk_file_size(paths: Iterable[str]) -> dict[str, int | None]:
    """Disk-truth size in bytes per library path (None = not on disk).

    A full scan: every path is answered (index hits, else a stat), because
    a size map has no way to say "not probed yet".
    """
    probed = bulk_probe_paths(paths, probe_mode=AvailabilityProbeMode.FULL_SCAN)
    return {path: result.materialised_size for path, result in probed.items()}


def bulk_file_exists(paths: Iterable[str]) -> dict[str, bool]:
    """Disk-truth existence per path; a full scan, see :func:`bulk_file_size`."""
    return {path: size is not None for path, size in bulk_file_size(paths).items()}


def local_paths(paths: Iterable[str | None]) -> list[str]:
    """The probe-able subset: non-empty, not a streaming URI."""
    return [path for path in paths if path and not is_streaming_path(path)]


def _location_paths(stable_ids: Sequence[str]) -> dict[str, list[str]]:
    if not stable_ids or not config.STATE_DB.exists():
        return {}
    state = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        return track_locations.list_location_paths(state, list(stable_ids))
    finally:
        state.close()


def classify_availability(
    path: str | None,
    results: Sequence[PathProbeResult | None],
    *,
    awaiting_volume: bool,
) -> FileAvailabilityStatus:
    """One row's status from its primary and alternate probe results.

    Any present copy wins; then any copy still being probed keeps the row
    pending; only then do the no-audio reasons apply. ``results`` must not
    include the primary when it sits on an unmounted volume (its stat there
    is meaningless).
    """
    statuses = {result.status for result in results if result is not None}
    if "present" in statuses:
        return "present"
    if PENDING in statuses:
        return PENDING
    if not path:
        return "absent"
    if is_streaming_path(path):
        return "streaming"
    if awaiting_volume:
        return "awaiting_volume"
    return "absent"


def _awaiting_volume(folder_by_sid: Mapping[str, str | None]) -> set[str]:
    mounted = remote_status.mounted_volumes()
    return {
        sid
        for sid, path in folder_by_sid.items()
        if path and remote_status.is_awaiting_volume(path, mounted=mounted)
    }


def _primary_results(
    folder_by_sid: Mapping[str, str | None],
    probed: Mapping[str, PathProbeResult],
    awaiting: set[str],
) -> dict[str, PathProbeResult | None]:
    """The primary path's probe per row; None for no local path, and for a
    path on an unmounted volume (a stat there says nothing)."""
    return {
        sid: probed.get(path) if path and sid not in awaiting else None
        for sid, path in folder_by_sid.items()
    }


def _is_present(result: PathProbeResult | None) -> bool:
    return result is not None and result.status == "present"


def sids_without_availability_row(stable_ids: Sequence[str]) -> set[str]:
    """Stable ids with no ``track_availability`` row (unchecked).

    No row is unknown, not present. A missing table or database treats every
    id as unchecked so a listing cannot promote it from an index hit.
    """
    wanted = list(dict.fromkeys(sid for sid in stable_ids if sid))
    if not wanted or not config.STATE_DB.exists():
        return set(wanted)
    conn = _open_ro(config.STATE_DB, "STATE_DB")
    known: set[str] = set()
    try:
        # One placeholder per id blows past SQLITE_LIMIT_VARIABLE_NUMBER
        # (999 on the packaged build). Playlist detail passes the whole
        # membership; chunk at the same bound as other bulk id lookups.
        for start in range(0, len(wanted), ID_BIND_BATCH):
            chunk = wanted[start : start + ID_BIND_BATCH]
            placeholders = ",".join("?" for _ in chunk)
            try:
                rows = conn.execute(
                    "SELECT stable_id FROM track_availability "
                    f"WHERE stable_id IN ({placeholders})",
                    chunk,
                ).fetchall()
            except Exception:
                return set(wanted)
            known.update(row[0] for row in rows)
    finally:
        conn.close()
    return {sid for sid in wanted if sid not in known}


def classify_rows(
    folder_by_sid: Mapping[str, str | None],
    probed: Mapping[str, PathProbeResult],
    budget: ProbeBudget,
    *,
    distrust_index_sids: set[str] | None = None,
) -> dict[str, FileAvailabilityStatus]:
    """Status per stable_id, consulting ``track_locations`` alternates for
    every row whose primary path is not already known present. Alternates
    draw on the SAME ``budget`` the primaries used.

    ``distrust_index_sids`` (unchecked rows) probe alternates with a real
    stat. A fresh index hit is not enough to call them present.
    """
    distrust = distrust_index_sids or set()
    awaiting = _awaiting_volume(folder_by_sid)
    primary = _primary_results(folder_by_sid, probed, awaiting)
    unresolved = [sid for sid, result in primary.items() if not _is_present(result)]
    alternates = _location_paths(unresolved)
    trusted = [
        path
        for sid, paths in alternates.items()
        if sid not in distrust
        for path in paths
    ]
    forced = [
        path
        for sid, paths in alternates.items()
        if sid in distrust
        for path in paths
    ]
    alt_probed = bulk_probe_paths(trusted, budget=budget)
    if forced:
        alt_probed.update(bulk_probe_paths(forced, budget=budget, trust_index=False))
    return {
        sid: classify_availability(
            path,
            [primary[sid], *(alt_probed.get(p) for p in alternates.get(sid, []))],
            awaiting_volume=sid in awaiting,
        )
        for sid, path in folder_by_sid.items()
    }


__all__ = [
    "PENDING",
    "PROBE_BUDGET_ROW_HYDRATION",
    "PROBE_BUDGET_TREE_SUMMARY",
    "AvailabilityProbeMode",
    "FileAvailabilityStatus",
    "PathProbeResult",
    "ProbeBudget",
    "bulk_file_exists",
    "bulk_file_size",
    "bulk_probe_paths",
    "classify_availability",
    "classify_rows",
    "local_paths",
    "sids_without_availability_row",
    "status_to_file_exists",
]
