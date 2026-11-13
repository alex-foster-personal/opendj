"""Budgeted disk-truth probing for listing rows (issue #1037, PERF-RB-01).

Answers "is this library path's audio on disk" from, in order: the in-process
L1 cache (``config._FILE_EXISTS_CACHE``), the persisted ``path_availability``
index (:mod:`.path_index`), and at most a per-request budget of real stats.
Whatever the budget cannot cover is handed to the background refresher. Row
hydration serves such a path's last known answer while it is re-checked, if
that answer is under :data:`SERVE_RECENT_MAX_S` old (PERF-RB-06: with a 30 s
TTL, shorter than one All Tracks walk, about 85% of page 1 read pending on the
silver preview). A path with no recent answer comes back
``AVAILABILITY_PENDING``; nothing is guessed in either direction.

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

import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Literal

from apps.adapters.rekordbox import config
from apps.adapters.rekordbox.errors import _open_ro
from apps.adapters.rekordbox.paths import is_streaming_path
from apps.shared import fs_residency, remote_status
from apps.shared.platform_paths import PathMap, load_path_map, may_remap, resolve_asset_path
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
#: How old a known answer may be and still be served while it is re-checked in
#: the background (PERF-RB-06). Twice the window in which an unchanged answer is
#: written back (PERF-RB-05), so a row that keeps being probed never ages out.
SERVE_RECENT_MAX_S: float = 2 * path_index.UNCHANGED_REWRITE_AFTER_S

# Opportunistic path_availability upsert on the listing GET. A long wait
# here stalls the pane and, past busy_timeout, used to 503 the whole
# playlist (OPEN-DJ-BE-8Y). The disk answers already live in L1.
PATH_INDEX_HYDRATE_BUSY_TIMEOUT_S: float = 0.25


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
    def serves_recent(self) -> bool:
        """Row hydration serves a known answer up to :data:`SERVE_RECENT_MAX_S`
        old instead of pending while the background refresher re-checks it."""
        return self.mode is AvailabilityProbeMode.ROW_HYDRATION

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
    """``resolved_path`` is the file ``materialised_size`` was measured at, or
    None when that cannot be proven without touching the disk. ``st_mtime_ns`` is
    from the same stat as ``materialised_size`` when present."""

    status: FileAvailabilityStatus
    materialised_size: int | None = None
    resolved_path: str | None = None
    st_mtime_ns: int | None = None


def _size_result(
    size: int | None,
    measured_path: str | None = None,
    mtime_ns: int | None = None,
) -> PathProbeResult:
    return PathProbeResult(
        "present" if size is not None else "absent",
        size,
        measured_path,
        mtime_ns,
    )


def _index_measured_path(path: str, path_map: PathMap) -> str | None:
    """The file an index answer measured, when ``path`` alone proves it. No I/O:
    an index row keeps no resolved path, so one a rule could send elsewhere is
    unproven until a stat this process made says where it landed."""
    return None if may_remap(path, path_map) else path


_PENDING_RESULT = PathProbeResult(PENDING)


def status_to_file_exists(status: FileAvailabilityStatus) -> bool | None:
    """Wire ``file_exists``: null exactly when availability is pending."""
    if status == PENDING:
        return None
    return status == "present"


def _stat_size(resolved: Path | None) -> tuple[int, int] | None:
    """Materialised ``(st_size, st_mtime_ns)``, or None when missing / not a file / dataless stub."""
    if resolved is None:
        return None
    return fs_residency.materialised_stat(resolved)


def _l1_hits(wanted: Sequence[str], now: float, namespace: str) -> dict[str, PathProbeResult]:
    """Answers this process stat within the TTL, under the current resolver: a
    path map that changed since then can land the same path on another file."""
    with config._FILE_EXISTS_LOCK:
        hits = {path: config._FILE_EXISTS_CACHE.get(path) for path in wanted}
    return {
        path: _size_result(hit[1], hit[2], hit[4])
        for path, hit in hits.items()
        if hit is not None and hit[3] == namespace and now - hit[0] < config.FILE_EXISTS_TTL_S
    }


def _state_db(state_db_path: Path | None) -> Path:
    """The caller's database, or the process default when it names none."""
    return config.STATE_DB if state_db_path is None else state_db_path


def _load_index(
    namespace: str,
    paths: Sequence[str],
    *,
    state_db_path: Path | None = None,
) -> dict[str, path_index.IndexEntry | None]:
    db_path = _state_db(state_db_path)
    if not paths or not db_path.exists():
        return {}
    conn = _open_ro(db_path, "STATE_DB")
    try:
        return path_index.bulk_lookup(conn, namespace, paths)
    finally:
        conn.close()


def _persist_path_stats(
    namespace: str,
    rows: Sequence[tuple[str, int | None, int | None]],
    *,
    state_db_path: Path | None = None,
) -> None:
    """Best-effort index write. Busy must not fail the listing that just statted."""
    db_path = _state_db(state_db_path)
    if not rows or not db_path.exists():
        return
    try:
        conn = state_db.open_rw(
            db_path,
            apply_schema=False,
            busy_timeout_s=PATH_INDEX_HYDRATE_BUSY_TIMEOUT_S,
        )
        try:
            with state_db.write_unit(conn, "path_avail_hydrate"):
                path_index.upsert_rows(conn, namespace, rows)
        finally:
            conn.close()
    except state_db.StateStoreBusyError:
        return
    except sqlite3.OperationalError as exc:
        if state_db.is_sqlite_busy(exc):
            return
        raise


def _record_stats(
    namespace: str,
    measured: Mapping[str, PathProbeResult],
    now: float,
    *,
    state_db_path: Path | None = None,
) -> None:
    """Cache this request's stats in L1, and persist them off the request thread.

    A read request never opens a write connection while the server runs
    (STATE-18): the running refresher persists the rows, so a writer holding
    the state.db lock cannot turn ``GET /api/v1/tracks`` into a 503. With no
    refresher running (CLI, script, no-lifespan test) the rows are written
    here, as before, because nothing else would write them. That inline write
    uses a short ``busy_timeout`` and ignores lock exhaustion (OPEN-DJ-BE-8Y):
    the disk-truth answers already live in L1.
    """
    if not measured:
        return
    with config._FILE_EXISTS_LOCK:
        for path, result in measured.items():
            config._FILE_EXISTS_CACHE[path] = (
                now,
                result.materialised_size,
                result.resolved_path,
                namespace,
                result.st_mtime_ns,
            )
    rows = [
        (path, result.materialised_size, result.st_mtime_ns)
        for path, result in measured.items()
    ]
    if path_availability_refresh.record(namespace, rows, state_db_path=state_db_path):
        return
    _persist_path_stats(
        namespace,
        rows,
        state_db_path=state_db_path,
    )


def _recent_answers(
    paths: Sequence[str],
    index: Mapping[str, path_index.IndexEntry | None],
    now: float,
    path_map: PathMap,
    namespace: str,
) -> dict[str, PathProbeResult]:
    """Answers no older than :data:`SERVE_RECENT_MAX_S`: this process's own
    expired L1 stat first (its time is exact), else a trusted index row. The
    index row's ``checked_at`` can lag the last stat by up to
    ``UNCHANGED_REWRITE_AFTER_S`` (PERF-RB-05), which the window covers."""
    with config._FILE_EXISTS_LOCK:
        l1 = {path: config._FILE_EXISTS_CACHE.get(path) for path in paths}
    wall_now = datetime.now(UTC)
    out: dict[str, PathProbeResult] = {}
    for path in paths:
        hit, entry = l1[path], index.get(path)
        if hit is not None and hit[3] == namespace and now - hit[0] < SERVE_RECENT_MAX_S:
            out[path] = _size_result(hit[1], hit[2], hit[4])
        elif entry is not None and (wall_now - entry.checked_at).total_seconds() < (
            SERVE_RECENT_MAX_S
        ):
            out[path] = _size_result(
                entry.materialised_size,
                _index_measured_path(path, path_map),
                entry.st_mtime_ns,
            )
    return out


def _triage(
    paths: Sequence[str],
    index: Mapping[str, path_index.IndexEntry | None],
    budget: ProbeBudget,
    out: dict[str, PathProbeResult],
    recent: Mapping[str, PathProbeResult],
    path_map: PathMap,
) -> tuple[list[str], list[str]]:
    """Answer fresh index hits into ``out``; split the rest into the paths
    this request stats now and the ones handed to the background refresher.
    Those are served stale for a tree summary, their recent answer for row
    hydration (PERF-RB-06), else pending."""
    stat_now: list[str] = []
    background: list[str] = []
    for path in paths:
        entry = index.get(path)
        if entry is not None and not entry.stale:
            out[path] = _size_result(
                entry.materialised_size,
                _index_measured_path(path, path_map),
                entry.st_mtime_ns,
            )
        elif budget.take():
            stat_now.append(path)
        else:
            background.append(path)
            if entry is not None and budget.serves_stale:
                out[path] = _size_result(
                    entry.materialised_size,
                    _index_measured_path(path, path_map),
                    entry.st_mtime_ns,
                )
            elif path in recent and budget.serves_recent:
                out[path] = recent[path]
            else:
                out[path] = _PENDING_RESULT
    return stat_now, background


def bulk_probe_paths(
    paths: Iterable[str],
    *,
    probe_mode: AvailabilityProbeMode = AvailabilityProbeMode.ROW_HYDRATION,
    budget: ProbeBudget | None = None,
    trust_index: bool = True,
    state_db_path: Path | None = None,
) -> dict[str, PathProbeResult]:
    """Disk truth or ``AVAILABILITY_PENDING`` for each unique local path.

    ``budget`` is the request's shared allowance; without one, a fresh
    budget for ``probe_mode`` is used (one call = one request).

    ``trust_index`` is false for tracks with no ``track_availability`` row.
    A copied ``path_availability`` index can mark another machine's path
    present with no stat here; those paths spend a budgeted stat, or come
    back pending. They are never reported present from the index alone.

    ``state_db_path`` is the database whose ``path_availability`` index this
    call reads and, when no refresher is running, writes. None is the
    process default.
    """
    wanted = list(dict.fromkeys(p for p in paths if p))
    if not wanted:
        return {}
    budget = budget if budget is not None else ProbeBudget(probe_mode)
    now = time.monotonic()
    namespace = path_index.resolver_namespace(config.DATA_DIR)
    out = _l1_hits(wanted, now, namespace)
    rest = [path for path in wanted if path not in out]
    if not rest:
        return out
    path_map = load_path_map(config.DATA_DIR)
    index = (
        _load_index(namespace, rest, state_db_path=state_db_path) if trust_index else {}
    )
    recent = (
        _recent_answers(rest, index, now, path_map, namespace) if budget.serves_recent else {}
    )
    stat_now, background = _triage(rest, index, budget, out, recent, path_map)
    # One map for the whole pass: a resolve with no map reloads and re-validates
    # path-map.json per path, which starves every other thread on a full scan.
    resolved = {
        path: resolve_asset_path(path, path_map=path_map).resolved for path in stat_now
    }
    measured: dict[str, PathProbeResult] = {}
    for path, target in resolved.items():
        stat = _stat_size(target)
        resolved_str = str(target) if target is not None else None
        if stat is None:
            measured[path] = _size_result(None, resolved_str, None)
        else:
            size, mtime_ns = stat
            measured[path] = _size_result(size, resolved_str, mtime_ns)
    out.update(measured)
    _record_stats(namespace, measured, now, state_db_path=state_db_path)
    if background:
        path_availability_refresh.schedule(background)
    return out


def bulk_file_size(
    paths: Iterable[str], *, state_db_path: Path | None = None
) -> dict[str, int | None]:
    """Disk-truth size in bytes per library path (None = not on disk).

    A full scan: every path is answered (index hits, else a stat), because
    a size map has no way to say "not probed yet". ``state_db_path`` selects
    the path index, same as :func:`bulk_probe_paths`.
    """
    probed = bulk_probe_paths(
        paths,
        probe_mode=AvailabilityProbeMode.FULL_SCAN,
        state_db_path=state_db_path,
    )
    return {path: result.materialised_size for path, result in probed.items()}


def bulk_file_exists(
    paths: Iterable[str], *, state_db_path: Path | None = None
) -> dict[str, bool]:
    """Disk-truth existence per path; a full scan, see :func:`bulk_file_size`."""
    return {
        path: size is not None
        for path, size in bulk_file_size(paths, state_db_path=state_db_path).items()
    }


def local_paths(paths: Iterable[str | None]) -> list[str]:
    """The probe-able subset: non-empty, not a streaming URI."""
    return [path for path in paths if path and not is_streaming_path(path)]


def _alternate_paths(
    folder_by_sid: Mapping[str, str | None],
    stable_ids: Sequence[str],
    *,
    state_db_path: Path | None = None,
) -> dict[str, list[str]]:
    """Every path but the primary from the ONE playable-candidate list the
    deck-load audio route also picks from (issue #3934)."""
    db_path = _state_db(state_db_path)
    if not stable_ids or not db_path.exists():
        return {}
    state = _open_ro(db_path, "STATE_DB")
    try:
        candidates = track_locations.playable_candidate_paths(
            state, {sid: folder_by_sid.get(sid) for sid in stable_ids}
        )
    finally:
        state.close()
    return {
        sid: [c.path for c in paths if c.source != "primary"]
        for sid, paths in candidates.items()
    }


def _first_present(results: Sequence[PathProbeResult | None]) -> PathProbeResult | None:
    """The copy a row uses: its primary when present, else its first present alternate."""
    return next((result for result in results if _is_present(result)), None)


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
    if _first_present(results) is not None:
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


def sids_without_availability_row(
    stable_ids: Sequence[str], *, state_db_path: Path | None = None
) -> set[str]:
    """Stable ids with no ``track_availability`` row (unchecked).

    No row is unknown, not present. A missing table or database treats every
    id as unchecked so a listing cannot promote it from an index hit.
    """
    wanted = list(dict.fromkeys(sid for sid in stable_ids if sid))
    db_path = _state_db(state_db_path)
    if not wanted or not db_path.exists():
        return set(wanted)
    conn = _open_ro(db_path, "STATE_DB")
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
    selected_probes: dict[str, PathProbeResult] | None = None,
    state_db_path: Path | None = None,
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
    alternates = _alternate_paths(folder_by_sid, unresolved, state_db_path=state_db_path)
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
    alt_probed = bulk_probe_paths(trusted, budget=budget, state_db_path=state_db_path)
    if forced:
        alt_probed.update(
            bulk_probe_paths(forced, budget=budget, trust_index=False, state_db_path=state_db_path)
        )
    results_by_sid = {
        sid: [primary[sid], *(alt_probed.get(p) for p in alternates.get(sid, []))]
        for sid in folder_by_sid
    }
    if selected_probes is not None:
        for sid, results in results_by_sid.items():
            if (selected := _first_present(results)) is not None:
                selected_probes[sid] = selected
    return {
        sid: classify_availability(path, results_by_sid[sid], awaiting_volume=sid in awaiting)
        for sid, path in folder_by_sid.items()
    }


__all__ = [
    "PENDING",
    "PATH_INDEX_HYDRATE_BUSY_TIMEOUT_S",
    "PROBE_BUDGET_ROW_HYDRATION",
    "PROBE_BUDGET_TREE_SUMMARY",
    "SERVE_RECENT_MAX_S",
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
