"""Path-map resolver fingerprint and stamp helpers for the availability worker.

Split from ``apps.engine_core.library_availability`` (#9721), which
re-exports every name here, so callers and tests keep importing from it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from apps.shared.platform_paths import SHARE_ROOT, PathMap, load_path_map
from apps.shared.state.locations import ID_BIND_BATCH
from apps.shared.state.machine_identity import get_or_create_machine_id

# Bump when classification changes in a way the incremental scan would not
# notice on its own. The scan only revisits unknown, stale, and
# awaiting_volume rows, so a persisted `absent` row stays absent until this
# generation or the loaded path map changes.
_AVAILABILITY_RESOLVER_GENERATION = 2
# Lives in state.db. A sidecar (state/availability-resolver.json) does not:
# restore workflows replace only state.db and would keep a matching sidecar
# over rows classified by an older resolver.
_RESOLVER_MARKER_PREFIX = "availability-resolver:"
# This is an invalidation marker, not a sync timestamp. The supported
# tracks.updated_at floor is year one (sync_stamp.FLOOR_STAMP), so neither
# 1970 nor that floor itself makes every row stale under strict `>`.
# Empty TEXT is non-NULL and sorts before every supported track timestamp.
_STALE_CHECKED_AT = ""


class _PathMapUnreadable(Exception):
    """path-map.json could not be read during a drain.

    The drain stays queued and retries. This must not escape to ``_run_loop``,
    which would park the worker in ``failed`` and suppress later wakes.
    """


def _load_path_map(data_dir: Path) -> PathMap:
    """Read path-map.json; raise ``_PathMapUnreadable`` on OSError/ValueError."""
    try:
        return load_path_map(data_dir)
    except (OSError, ValueError) as exc:
        raise _PathMapUnreadable(str(exc)) from exc


def _resolver_env_fingerprint() -> dict[str, str]:
    """Resolver inputs that change targets without a path-map edit.

    ``resolve_library_path`` sends ``/PIONEER/...`` through ``SHARE_ROOT``
    and, when ``MDT_LIBRARY_MODE`` is remote, refuses a native ``/Users/...``
    path. A restart that changes the mode, ``MDT_CRATE_ROOT``, or the share
    root this process imported must not keep settled rows.
    """
    return {
        "crate_root": os.environ.get("MDT_CRATE_ROOT", "").strip(),
        "library_mode": os.environ.get("MDT_LIBRARY_MODE", "").strip().lower(),
        "share_root": str(SHARE_ROOT),
    }


def _resolver_fingerprint_from_map(data_dir: Path, path_map: PathMap) -> str:
    """Fingerprint for an already-read map. Includes this machine's id."""
    env_inputs = _resolver_env_fingerprint()
    payload = {
        "crate_root": env_inputs["crate_root"],
        "entries": [{"from": src, "to": dst} for src, dst in path_map.entries],
        "generation": _AVAILABILITY_RESOLVER_GENERATION,
        "library_mode": env_inputs["library_mode"],
        "machine_id": get_or_create_machine_id(data_dir),
        "share_root": env_inputs["share_root"],
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolver_fingerprint(data_dir: Path) -> str:
    """Stable id of this machine, the path map, the resolver generation, and
    the mode, crate root, and share root the resolver is using.

    Machine id lives outside state.db. A restored database must not keep
    another machine's present or absent verdicts just because the path map
    matches, including the common case of an empty map. Mode and crate root
    are the same kind of input: they change ``/PIONEER`` and remote
    ``/Users`` targets while the map file stays byte-identical.
    """
    return _resolver_fingerprint_from_map(data_dir, _load_path_map(data_dir))


def _resolver_marker(fingerprint: str) -> str:
    return f"{_RESOLVER_MARKER_PREFIX}{fingerprint}"


def _stored_resolver_fingerprint(conn: sqlite3.Connection) -> str | None:
    """The fingerprint stored in this database, or None when it must be reset.

    Zero or several markers both mean "not bound to this file". A restored
    state.db brings its own marker (or none); a leftover sidecar is ignored.
    """
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("schema_meta_markers",),
    ).fetchone()
    if present is None:
        raise RuntimeError("schema_meta_markers is missing; cannot bind the availability resolver stamp to state.db")
    rows = conn.execute(
        "SELECT marker FROM schema_meta_markers WHERE marker LIKE ?",
        (_RESOLVER_MARKER_PREFIX + "%",),
    ).fetchall()
    if len(rows) != 1:
        return None
    marker = str(rows[0][0])
    if not marker.startswith(_RESOLVER_MARKER_PREFIX):
        return None
    fingerprint = marker[len(_RESOLVER_MARKER_PREFIX) :]
    if not fingerprint:
        return None
    return fingerprint


def _stale_availability_except(conn: sqlite3.Connection, preserve_ids: tuple[str, ...]) -> int:
    """Stale every availability row whose id is not in ``preserve_ids``.

    One ``NOT IN`` of the whole preserve list binds two timestamps plus one
    variable per id. A first empty-map round can preserve ``MAX_ROUND_IDS``
    (2000) ids, and SQLite builds capped at 999 variables raise "too many
    SQL variables". Chunking that ``NOT IN`` is not the same statement: an
    earlier chunk would stale ids that belong in a later chunk. Select the
    complement, then update it in ``IN`` chunks of ``ID_BIND_BATCH - 2`` so
    each statement binds the two timestamps plus that chunk and nothing more.
    """
    chunk_len = ID_BIND_BATCH - 2
    if chunk_len < 1:
        raise RuntimeError(f"ID_BIND_BATCH ({ID_BIND_BATCH}) cannot fit two timestamps plus one stable_id")
    preserve = set(preserve_ids)
    to_stale = [
        stable_id
        for (stable_id,) in conn.execute(
            "SELECT stable_id FROM track_availability WHERE checked_at != ? ORDER BY stable_id",
            (_STALE_CHECKED_AT,),
        )
        if stable_id not in preserve
    ]
    changed = 0
    for start in range(0, len(to_stale), chunk_len):
        chunk = to_stale[start : start + chunk_len]
        placeholders = ",".join("?" * len(chunk))
        before = conn.total_changes
        conn.execute(
            f"UPDATE track_availability SET checked_at = ? WHERE checked_at != ? AND stable_id IN ({placeholders})",
            (_STALE_CHECKED_AT, _STALE_CHECKED_AT, *chunk),
        )
        changed += conn.total_changes - before
    return changed
