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

A row mapped to rekordbox that this machine's own locations and
``tracks.file_path`` do not resolve is also checked at its rekordbox FolderPath
(LIBM-184), so a track the playlist badges and the deck count as playable from
that copy is ``present`` here too. The order is the deck's: believed state
first, the FolderPath only when that has no copy here
(``apps.adapters.rekordbox.paths``), so :func:`local_audio_paths` names the
file the deck would play. Among an unresolved row's candidates for bucketing,
the FolderPath comes first as in :func:`state_locations.playable_candidate_paths`.
The master database is read only for rows that did not resolve.

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
  ✔︎ ✅ 🎯 a rekordbox FolderPath copy counts (LIBM-184)
    [if] a mapped row's only copy here is at its FolderPath [then] present
    [if] that FolderPath is gone too [then] the row is not present
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

from apps.shared import platform_paths, remote_status
from apps.shared.state import locations as state_locations
from apps.shared.state import sync_stamp
from apps.sync_hub import config as sync_config
from apps.webui.server.rb_vendor_pkg import track_rows
from apps.webui.soft_deletes import has_soft_deletes

DECK_LOAD_REFUSAL_STREAMING = "streaming"
DECK_LOAD_REFUSAL_CLOUD_POLICY = "cloud_policy_unconfigured"

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


def _rekordbox_folder_paths(
    conn: sqlite3.Connection, stable_ids: Sequence[str]
) -> dict[str, str]:
    """Rekordbox FolderPath per mapped id, from the same reader the playlist
    badges use (:func:`track_rows.rb_meta_from_state`) over this scan's own
    connection. Ids with no mapping or no FolderPath are absent.

    No rekordbox install here (``MASTER_DB_UNAVAILABLE``: a synced state.db
    whose mappings came from another machine) means no FolderPath copy, the
    same reading the deck's audio resolver gives it
    (``apps.adapters.rekordbox.paths._rekordbox_copy``), so the scan still
    buckets every row. Any other failure propagates: it means this machine
    could not look, not that there is no copy."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'track_vendor_ids'"
    ).fetchone()
    if has_table is None:
        return {}
    try:
        metas = track_rows.rb_meta_from_state(conn, stable_ids)
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("code") == "MASTER_DB_UNAVAILABLE":
            return {}
        raise
    return {
        sid: meta.folder_path
        for sid, meta in metas.items()
        if meta.folder_path and meta.folder_path.strip()
    }


def _path_candidates(
    file_path: object, alternates: Sequence[str], folder_path: str | None = None
) -> list[str]:
    """Rekordbox's FolderPath first when mapped, then the row's own path, then
    each alternate location, each listed once."""
    raw = [folder_path] if folder_path else []
    if file_path and str(file_path).strip() and str(file_path) not in raw:
        raw.append(str(file_path))
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
def _resolve_here(
    conn: sqlite3.Connection, stable_ids: Sequence[str], owner: str
) -> tuple[dict[str, Path | None], dict[str, str]]:
    """This machine's file per id: its own locations first, then, for an id
    those leave unresolved, its rekordbox FolderPath when that is on disk here
    (LIBM-184). Also returns the FolderPaths read, for bucketing the rest."""
    resolved = state_locations.bulk_local_audio_paths(
        conn, list(stable_ids), machine_id=owner or None
    )
    unresolved = [sid for sid in stable_ids if resolved.get(sid) is None]
    rekordbox_folders = _rekordbox_folder_paths(conn, unresolved)
    for stable_id, folder in rekordbox_folders.items():
        if not _is_streaming(folder):
            here = state_locations.mapped_audio_path_if_materialised(folder)
            if here is not None:
                resolved[stable_id] = here
    return resolved, rekordbox_folders


def local_audio_paths(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> dict[str, Path]:
    """`stable_id -> local file` for the given ids that are present on this
    machine: the same resolution `scan_playability` uses for its `present`
    bucket (:func:`_resolve_here`), without classifying the rest of the library."""
    resolved, _folders = _resolve_here(conn, stable_ids, _owner_machine_id(conn, None))
    return {sid: Path(path) for sid, path in resolved.items() if path is not None}


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
    resolved, rekordbox_folders = _resolve_here(conn, stable_ids, owner)
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
        candidates = _path_candidates(
            file_path, alternates.get(stable_id, []), rekordbox_folders.get(stable_id)
        )
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


def audio_policy_configured(conn: sqlite3.Connection, machine_id: str) -> bool:
    """True when this machine has a live ``sync_policies`` audio cell."""
    row = conn.execute(
        "SELECT 1 FROM sync_policies "
        "WHERE machine_id = ? AND asset_kind = 'audio' AND deleted_at IS NULL "
        "LIMIT 1",
        (machine_id,),
    ).fetchone()
    return row is not None


def cloudsync_enabled(data_dir: Path) -> bool:
    """Whether CloudSync is turned on for this data directory."""
    try:
        return sync_config.resolve_config(data_dir).enabled
    except sync_config.CloudSyncConfigError:
        return False


def deck_load_refusal(
    *,
    local_present: bool,
    is_streaming: bool,
    is_remote: bool,
    has_remote_copy: bool,
    cloudsync_on: bool,
    policy_configured: bool,
) -> str | None:
    """Why a listing row must not reach a deck on this host, or None when it can."""
    if local_present:
        return None
    if is_streaming:
        return DECK_LOAD_REFUSAL_STREAMING
    if cloudsync_on and not policy_configured and (is_remote or has_remote_copy):
        return DECK_LOAD_REFUSAL_CLOUD_POLICY
    return None


__all__ = [
    "BUCKETS",
    "DECK_LOAD_REFUSAL_CLOUD_POLICY",
    "DECK_LOAD_REFUSAL_STREAMING",
    "LibraryPlayability",
    "absent_folder",
    "audio_policy_configured",
    "cloudsync_enabled",
    "deck_load_refusal",
    "scan_playability",
]
