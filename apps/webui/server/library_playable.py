"""The ONE playable predicate: which library rows have audio on THIS machine.

Reconcile's summary and ingest coverage each used to decide "playable" on
their own, and disagreed (2685 against 1184 on the same library, Thu 1 Oct
2026). Both now read this module, so the two numbers are one measurement.

Every live ``tracks`` row lands in exactly one bucket, and the scan asserts
the buckets sum to the row total before it returns:

==================  =========================================================
bucket              meaning
==================  =========================================================
``present``         audio resolves here right now. The honest denominator.
``broken_here``     this machine recorded the file as existing and it no
                    longer resolves: a link that SHOULD work here and does
                    not. The only bucket that makes the health light amber.
``off_machine``     no audio here and no evidence this machine ever held it
                    (a row synced in from another machine, or a path from a
                    home directory that never existed here). Not broken.
``awaiting_volume`` the path is under an unmounted ``/Volumes/<name>``.
``streaming``       a service URI. There is no file to be missing.
``pathless``        no recorded path at all.
==================  =========================================================

Evidence for ``broken_here`` is a ``track_locations`` row owned by this
machine with ``available = 1``: this machine stat'ed the file and found it.
On a schema with no per-machine location rows (pre-v6) a database cannot hold
another machine's rows, so every unresolved local path there is broken here.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 every row lands in exactly one bucket
    [if] bucket counts do not sum to the live row total [then ⛔️] raise
    [if] a soft-deleted row exists [then] it is in no bucket
  ✔︎ ✅ 🎯 off-machine rows are not broken
    [if] a row has no audio here and no this-machine evidence [then] off_machine
    [if] only ANOTHER machine recorded the file as available [then] off_machine
  ✔︎ ✅ 🎯 a genuinely broken local link stays visible (overshoot control)
    [if] this machine recorded the file as available and it is gone
    [then] broken_here, never off_machine
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from apps.shared import platform_paths, remote_status
from apps.shared.state import locations as state_locations
from apps.shared.state import sync_stamp
from apps.webui.soft_deletes import has_soft_deletes

BUCKETS: tuple[str, ...] = (
    "present",
    "broken_here",
    "off_machine",
    "awaiting_volume",
    "streaming",
    "pathless",
)


@dataclass(frozen=True)
class LibraryPlayability:
    """One snapshot of where every live row's audio stands on this machine."""

    total: int
    #: (stable_id, resolved local path) for every playable row, in row order.
    present: tuple[tuple[str, str], ...]
    broken_here: tuple[str, ...]
    off_machine: int
    awaiting_volume: int
    streaming: int
    pathless: int
    #: (folder, rows) for every row whose file is not on this Mac (broken_here
    #: plus off_machine), largest first (ENRICH-02). ``absent_folder`` names it.
    absent_folders: tuple[tuple[str, int], ...] = ()

    def counts(self) -> dict[str, int]:
        return {
            "total": self.total,
            "present": len(self.present),
            "broken_here": len(self.broken_here),
            "off_machine": self.off_machine,
            "awaiting_volume": self.awaiting_volume,
            "streaming": self.streaming,
            "pathless": self.pathless,
        }


#-----------------------------------------------------------------------------
# helpers
#-----------------------------------------------------------------------------
def _is_streaming(path: str) -> bool:
    """A service URI, by the same two tests the rest of the app applies."""
    if path.startswith(platform_paths.STREAMING_PREFIXES):
        return True
    head, sep, rest = path.partition(":")
    if path.startswith("/") or not sep or not rest:
        return False
    # A Windows drive letter is one character; a scheme is two or more.
    return len(head) >= 2 and head.replace("+", "").replace("-", "").isalnum() and (
        not head[0].isdigit()
    )


def _claimed_here(
    conn: sqlite3.Connection, stable_ids: Sequence[str], machine_id: str
) -> set[str] | None:
    """Ids this machine once confirmed on disk, or None when unknowable.

    None means the schema carries no per-machine evidence at all, which is a
    different answer from "nothing was claimed" and must not be read as one.
    """
    if not state_locations._locations_machine_scoped(conn):
        return None
    claimed: set[str] = set()
    for batch in state_locations._batched(list(stable_ids), state_locations.ID_BIND_BATCH):
        placeholders = ",".join("?" * len(batch))
        for (stable_id,) in conn.execute(
            f"SELECT DISTINCT stable_id FROM track_locations "
            f"WHERE stable_id IN ({placeholders}) AND machine_id = ? "
            f"AND deleted_at IS NULL AND kind = 'local' AND available = 1",
            (*batch, machine_id),
        ):
            claimed.add(str(stable_id))
    return claimed


def _unresolved_bucket(
    stable_id: str,
    candidates: Sequence[str],
    claimed: set[str] | None,
    mounted: set[str],
) -> str:
    """Bucket for a row whose audio did not resolve on this machine."""
    if not candidates:
        return "pathless"
    if all(_is_streaming(path) for path in candidates):
        return "streaming"
    local = [path for path in candidates if not _is_streaming(path)]
    if any(remote_status.is_awaiting_volume(path, mounted=mounted) for path in local):
        return "awaiting_volume"
    if claimed is None or stable_id in claimed:
        return "broken_here"
    return "off_machine"


def _live_track_rows(conn: sqlite3.Connection) -> list[tuple[str, object]]:
    tracks_sql = (
        "SELECT stable_id, file_path FROM tracks WHERE deleted_at IS NULL"
        if has_soft_deletes(conn, "tracks")
        else "SELECT stable_id, file_path FROM tracks"
    )
    return [(str(sid), fp) for sid, fp in conn.execute(tracks_sql)]


def _owner_machine_id(conn: sqlite3.Connection, machine_id: str | None) -> str:
    """The machine whose locations count, or "" when the schema has none."""
    return machine_id or (
        sync_stamp.local_machine_id(conn)
        if state_locations._locations_machine_scoped(conn)
        else ""
    )


def _path_candidates(file_path: object, alternates: Sequence[str]) -> list[str]:
    """The row's own path first, then each alternate location not already listed."""
    raw = [str(file_path)] if file_path and str(file_path).strip() else []
    return [*raw, *(p for p in alternates if p not in raw)]


def absent_folder(path: str, home: str) -> str:
    """The folder a missing file is reported under: two levels below a home
    directory (``~/Music/Convert``, ``/Users/dev/Documents/TuneFab``), the
    drive for ``/Volumes/<name>``, else the first two levels."""
    parts = [part for part in PurePosixPath(path).parent.parts if part != "/"]
    home_parts = [part for part in PurePosixPath(home).parts if part != "/"]
    if home_parts and parts[: len(home_parts)] == home_parts:
        return "/".join(["~", *parts[len(home_parts):][:2]])
    if parts[:1] == ["Users"] and len(parts) > 1:
        return "/" + "/".join(parts[:4])
    if parts[:1] == ["Volumes"]:
        return "/" + "/".join(parts[:2])
    return "/" + "/".join(parts[:2])


def _require_buckets_cover_every_row(scan: LibraryPlayability) -> None:
    bucketed = sum(count for name, count in scan.counts().items() if name != "total")
    if bucketed != scan.total:
        raise RuntimeError(
            f"playability buckets sum to {bucketed}, not the {scan.total} live rows"
        )


#-----------------------------------------------------------------------------
# the scan
#-----------------------------------------------------------------------------
def scan_playability(
    conn: sqlite3.Connection,
    *,
    machine_id: str | None = None,
    mounted: set[str] | None = None,
) -> LibraryPlayability:
    """Classify every live track row. Read-only; stats paths, never audio."""
    rows = _live_track_rows(conn)
    stable_ids = [sid for sid, _fp in rows]
    owner = _owner_machine_id(conn, machine_id)
    resolved = state_locations.bulk_local_audio_paths(
        conn, stable_ids, machine_id=owner or None
    )
    unresolved = [sid for sid in stable_ids if resolved.get(sid) is None]
    alternates = (
        state_locations.list_location_paths(conn, unresolved, machine_id=owner)
        if owner
        else {}
    )
    claimed = _claimed_here(conn, unresolved, owner)
    volumes = remote_status.mounted_volumes() if mounted is None else mounted

    present: list[tuple[str, str]] = []
    broken_here: list[str] = []
    tally = {"off_machine": 0, "awaiting_volume": 0, "streaming": 0, "pathless": 0}
    folders: Counter[str] = Counter()
    home = str(Path.home())
    for stable_id, file_path in rows:
        path = resolved.get(stable_id)
        if path is not None:
            present.append((stable_id, str(path)))
            continue
        candidates = _path_candidates(file_path, alternates.get(stable_id, []))
        bucket = _unresolved_bucket(stable_id, candidates, claimed, volumes)
        if bucket in ("broken_here", "off_machine"):
            folders[absent_folder(next(p for p in candidates if not _is_streaming(p)), home)] += 1
        if bucket == "broken_here":
            broken_here.append(stable_id)
        else:
            tally[bucket] += 1

    scan = LibraryPlayability(
        total=len(rows),
        present=tuple(present),
        broken_here=tuple(broken_here),
        off_machine=tally["off_machine"],
        awaiting_volume=tally["awaiting_volume"],
        streaming=tally["streaming"],
        pathless=tally["pathless"],
        absent_folders=tuple(sorted(folders.items(), key=lambda item: (-item[1], item[0]))),
    )
    _require_buckets_cover_every_row(scan)
    return scan


__all__ = ["BUCKETS", "LibraryPlayability", "absent_folder", "scan_playability"]
