"""Multiple playable locations per track, and the backend picker.

``tracks.file_path`` stays the ingest/legacy primary path. This table holds
extra copies: another machine, a remote URL, a lower-bitrate transcode.
The play path picks one winner. The frontend is never given the list.

Pick order (fail-closed, no invented fallbacks that hide a miss):

1. Drop broken (not materialised after path-map).
2. Prefer ``local`` over ``remote`` when any local copy works.
3. Apply the configured venue window (min/max rank). If that window is
   empty, use the working pool rather than 404 a playable file.
4. Highest venue rank wins; ties keep insertion order.

Per-machine since schema v6 / ADR 08 point 1: ``file_path``, ``available``,
``probed_at`` and ``venue_*`` describe one machine's disk, so every row
carries the ``machine_id`` that observed it and every read here filters to
the local machine. Rows synced in from the fleet stay visible to an explicit
cross-machine query ("what is where") but can never be counted as local
availability -- that was round 1 finding 7a, and it is the repo's
HONEST DENOMINATORS rule in schema form.
"""
from __future__ import annotations

import os
import sqlite3
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apps.shared import audio_quality, fs_residency, platform_paths
from apps.shared.platform_paths import PathMap

from . import sync_stamp as _sync_stamp

Kind = Literal["local", "remote"]
Role = Literal["primary", "alternate"]

AUDIO_MEDIA_TYPES: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
}

MIN_VENUE_ENV = "MDT_AUDIO_MIN_VENUE"
MAX_VENUE_ENV = "MDT_AUDIO_MAX_VENUE"
SHARE_MAX_VENUE_ENV = "MDT_AUDIO_SHARE_MAX_VENUE"


class LocationError(ValueError):
    """Invalid location row or pick policy."""


@dataclass(frozen=True)
class TrackLocation:
    # ``location_id`` (uuid4 hex) replaced the v4 INTEGER AUTOINCREMENT key
    # in schema v6 because integer keys collide across machines. The rowid is
    # gone from this dataclass entirely: it is stable only within one DB file,
    # so exposing it invited call sites that could not survive a sync.
    location_id: str
    machine_id: str
    stable_id: str
    kind: Kind
    role: Role
    file_path: str | None
    remote_url: str | None
    venue_key: str | None
    venue_rank: int | None
    available: bool
    probed_at: str | None
    content_hash: str | None


@dataclass(frozen=True)
class PickPolicy:
    """Venue window the picker may choose inside.

    Ranks come from ``apps.shared.audio_quality.VENUES`` (0 naughty_step
    through 5 stadium). ``prefer_local`` is the local>remote rule.
    """

    prefer_local: bool = True
    min_venue_rank: int = 0
    max_venue_rank: int = 5


@dataclass(frozen=True)
class PickedAudio:
    path: Path
    media_type: str
    kind: Kind
    venue_key: str | None
    venue_rank: int | None
    source: str


@dataclass(frozen=True)
class _Candidate:
    kind: Kind
    path: Path
    media_type: str
    venue_key: str | None
    venue_rank: int | None
    source: str
    working: bool


def policy_from_env(*, share: bool = False) -> PickPolicy:
    """Build a pick policy from env. Unknown venue keys fail loud."""
    min_key = os.environ.get(MIN_VENUE_ENV, "naughty_step")
    default_max = "warehouse" if share else "stadium"
    max_key = os.environ.get(
        SHARE_MAX_VENUE_ENV if share else MAX_VENUE_ENV,
        default_max,
    )
    return PickPolicy(
        prefer_local=True,
        min_venue_rank=_venue_rank(min_key),
        max_venue_rank=_venue_rank(max_key),
    )


def _venue_rank(key: str) -> int:
    venue = audio_quality.BY_KEY.get(key)
    if venue is None:
        known = ", ".join(v.key for v in audio_quality.VENUES)
        raise LocationError(f"unknown venue {key!r}; expected one of: {known}")
    return venue.rank


def normalize_stored_text(value: str | None) -> str | None:
    """NFC-normalize a path or URL on its way into ``track_locations``.

    Round 2 finding N2. ADR 08 point 6a normalizes strings on the WIRE and in
    the digest, deliberately not in storage -- which left the natural-key
    lookup below comparing raw bytes. macOS hands back NFD from the
    filesystem while rekordbox and the Windows/Linux machines hand back NFC,
    so one file acquired two ``track_locations`` rows on ONE machine; both
    canonicalized to the same bytes on the wire, the hub collapsed them to
    one and pruned the loser's changelog entries, the spoke kept two, and
    every later sync failed its digest compare on ``track_locations``
    permanently with no repair path.

    Normalizing here, at the storage boundary, makes the two spellings one
    row before the UNIQUE index ever sees them. It is the same normalization
    ``apps/reconcile/locate.py``, ``apps/reconcile/index_disk.py`` and
    ``apps/sync/matcher.py`` already apply for matching; this module was the
    outlier.

    Caveat worth knowing: on a byte-exact filesystem (Linux) a file created
    with NFD bytes will not open under its NFC spelling. That is why
    :func:`_probe_file` probes the NORMALIZED path -- the row's stored path
    and its ``available`` flag then describe the same string, so a spelling
    the machine cannot open reads as unavailable rather than as a path that
    lies.
    """
    if value is None:
        return None
    return unicodedata.normalize("NFC", value)


def locations_table_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_locations'"
    ).fetchone()
    return row is not None


def _locations_machine_scoped(conn: sqlite3.Connection) -> bool:
    """True when ``track_locations`` carries per-machine ``machine_id`` (schema v6+)."""
    if not locations_table_ready(conn):
        return False
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(track_locations)")
    }
    return "machine_id" in columns


def _tracks_soft_deletes(conn: sqlite3.Connection) -> bool:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
    return "deleted_at" in columns


def list_locations(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    machine_id: str | None = None,
) -> list[TrackLocation]:
    """This machine's location rows for ``stable_id``, oldest first.

    ``machine_id`` defaults to the local machine (resolved from the
    connection's data dir). Pass another machine's id to ask what the fleet
    believes IT holds; never pass one to compute local availability.
    """
    if not locations_table_ready(conn):
        return []
    owner = machine_id or _sync_stamp.local_machine_id(conn)
    rows = conn.execute(
        "SELECT location_id, machine_id, stable_id, kind, role, file_path, "
        "remote_url, venue_key, venue_rank, available, probed_at, "
        "content_hash FROM track_locations "
        # `OR machine_id IS NULL` is load-bearing, not defensive breadth: a row
        # that escaped the v6 backfill belongs to no machine, so an owner-only
        # filter drops it SILENTLY and this accessor returns a short list that
        # reads as a complete one. Selecting it lets _row_to_location refuse it
        # with the runbook message, which is the behavior this module promises.
        # Codex found the guard unreachable on #701.
        "WHERE stable_id = ? AND (machine_id = ? OR machine_id IS NULL) "
        "ORDER BY created_at, location_id",
        (stable_id, owner),
    ).fetchall()
    return [_row_to_location(row) for row in rows]


#: Ids bound per statement in a bulk read. SQLite caps host parameters at
#: ``SQLITE_LIMIT_VARIABLE_NUMBER``, which is 32766 on 3.32+ and **999** on
#: everything older - including the sqlite3 that ships with some system
#: Pythons and with the packaged desktop build. A caller that passes the
#: whole library therefore works on the dev machine and raises "too many SQL
#: variables" on a user's, which is the worst shape a limit can have. 500
#: leaves room for the other bound values in any statement that uses this.
ID_BIND_BATCH: int = 500


def _batched(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    """Yield ``items`` in slices of at most ``size``. Never empty."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


ResidencyPredicate = Callable[[Path], bool]


def _materialise_raw_audio_path(
    raw: str | None,
    *,
    path_map: PathMap | None = None,
    residency: ResidencyPredicate = fs_residency.is_materialised,
) -> Path | None:
    if not raw or raw.startswith(platform_paths.STREAMING_PREFIXES):
        return None
    mapped = platform_paths.resolve_asset_path(raw, path_map=path_map)
    if mapped.resolved is None or not residency(mapped.resolved):
        return None
    return mapped.resolved


#: Candidate order for a machine's local location rows: a row probed
#: available first, then primary before alternate, then oldest.
_LOCAL_LOCATION_ORDER = (
    "available DESC, CASE role WHEN 'primary' THEN 0 ELSE 1 END, "
    "created_at, location_id"
)


def _local_audio_raw_candidates(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    machine_id: str,
) -> list[tuple[str, Literal["location", "track_path"]]]:
    """Ordered raw path candidates for ``stable_id`` on this machine.

    EVERY local location row on this machine is a candidate, available rows
    first, then primary before alternate. A primary whose file is gone (an
    imported path from another Mac) must not shadow a relinked alternate
    that is on disk: callers take the first candidate that materialises.
    """
    candidates: list[tuple[str, Literal["location", "track_path"]]] = []
    if _locations_machine_scoped(conn):
        rows = conn.execute(
            f"SELECT file_path FROM track_locations "
            f"WHERE stable_id = ? AND machine_id = ? AND deleted_at IS NULL "
            f"AND kind = 'local' AND file_path IS NOT NULL "
            f"ORDER BY {_LOCAL_LOCATION_ORDER}",
            (stable_id, machine_id),
        ).fetchall()
        for (file_path,) in rows:
            if file_path and not any(raw == str(file_path) for raw, _ in candidates):
                candidates.append((str(file_path), "location"))
    tracks_sql = (
        "SELECT file_path FROM tracks WHERE stable_id = ? AND deleted_at IS NULL"
        if _tracks_soft_deletes(conn)
        else "SELECT file_path FROM tracks WHERE stable_id = ?"
    )
    row = conn.execute(tracks_sql, (stable_id,)).fetchone()
    if row is not None and row[0]:
        track_path = str(row[0])
        if not any(raw == track_path for raw, _ in candidates):
            candidates.append((track_path, "track_path"))
    return candidates


def local_audio_path(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    machine_id: str | None = None,
    path_map: PathMap | None = None,
    residency: ResidencyPredicate = fs_residency.is_materialised,
) -> Path | None:
    """This machine's materialised local audio path for ``stable_id``.

    Prefers a local ``track_locations`` row on this machine, then
    ``tracks.file_path``. Returns ``None`` when neither path materialises.
    ``residency`` is the on-disk gate: the default admits regular files with
    local bytes; a playback caller that probes ``open()`` itself passes
    :func:`fs_residency.exists_for_audio_open_probe` so a FIFO or other
    special node reaches the bounded open probe instead of reading as absent.
    """
    owner = machine_id or _sync_stamp.local_machine_id(conn)
    for raw, _source in _local_audio_raw_candidates(conn, stable_id, machine_id=owner):
        resolved = _materialise_raw_audio_path(raw, path_map=path_map, residency=residency)
        if resolved is not None:
            return resolved
    return None


def recorded_audio_path(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    machine_id: str | None = None,
) -> str | None:
    """This machine's first RECORDED audio path for ``stable_id``, unprobed.

    Same candidate order as :func:`local_audio_path` (a local
    ``track_locations`` row on this machine, then ``tracks.file_path``) but
    without the residency gate, for callers that probe the path themselves
    and must tell "no recorded location" from "recorded but missing or
    blocked on disk" (the preflight audio-access row).
    """
    owner = machine_id or _sync_stamp.local_machine_id(conn)
    candidates = _local_audio_raw_candidates(conn, stable_id, machine_id=owner)
    return candidates[0][0] if candidates else None


def local_audio_path_raw(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    machine_id: str | None = None,
    path_map: PathMap | None = None,
) -> tuple[str | None, Literal["location", "track_path"]]:
    """First materialised raw path and which layer supplied it."""
    owner = machine_id or _sync_stamp.local_machine_id(conn)
    for raw, source in _local_audio_raw_candidates(conn, stable_id, machine_id=owner):
        if _materialise_raw_audio_path(raw, path_map=path_map) is not None:
            return (raw, source)
    return (None, "track_path")


def _local_location_paths(
    conn: sqlite3.Connection, stable_ids: Sequence[str], machine_id: str,
) -> dict[str, list[str]]:
    """Per id, every local location path on ``machine_id``, in candidate order.

    Same order as the per-id reader (:func:`_local_audio_raw_candidates`), so
    a listed row and its single-track routes agree on which file plays.
    """
    location_by_id: dict[str, list[str]] = {}
    if not _locations_machine_scoped(conn):
        return location_by_id
    for batch in _batched(list(stable_ids), ID_BIND_BATCH):
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT stable_id, file_path FROM track_locations "
            f"WHERE stable_id IN ({placeholders}) AND machine_id = ? "
            f"AND deleted_at IS NULL AND kind = 'local' "
            f"AND file_path IS NOT NULL "
            f"ORDER BY stable_id, {_LOCAL_LOCATION_ORDER}",
            (*batch, machine_id),
        ).fetchall()
        for stable_id, file_path in rows:
            paths = location_by_id.setdefault(str(stable_id), [])
            if file_path and str(file_path) not in paths:
                paths.append(str(file_path))
    return location_by_id


def bulk_local_audio_paths(
    conn: sqlite3.Connection,
    stable_ids: Sequence[str],
    *,
    machine_id: str | None = None,
    path_map: PathMap | None = None,
) -> dict[str, Path | None]:
    """Materialised local audio paths for many ``stable_ids`` on this machine.

    Answers exactly what :func:`local_audio_path` answers per id (the listing
    batches through this, and a listed row must agree with its single-track
    routes): each local location row in :func:`_local_audio_raw_candidates`
    order, then ``tracks.file_path``; the first that materialises wins.
    """
    out: dict[str, Path | None] = {sid: None for sid in stable_ids}
    if not stable_ids:
        return out
    owner = machine_id or _sync_stamp.local_machine_id(conn)
    location_by_id = _local_location_paths(conn, stable_ids, owner)
    track_paths: dict[str, str | None] = {}

    tracks_deleted_filter = (
        " AND deleted_at IS NULL" if _tracks_soft_deletes(conn) else ""
    )
    for batch in _batched(list(stable_ids), ID_BIND_BATCH):
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT stable_id, file_path FROM tracks "
            f"WHERE stable_id IN ({placeholders}){tracks_deleted_filter}",
            batch,
        ).fetchall()
        for stable_id, file_path in rows:
            track_paths[str(stable_id)] = str(file_path) if file_path else None

    for sid in stable_ids:
        for raw in (*location_by_id.get(sid, ()), track_paths.get(sid)):
            if not raw:
                continue
            resolved = _materialise_raw_audio_path(raw, path_map=path_map)
            if resolved is not None:
                out[sid] = resolved
                break
    return out


def list_location_paths(
    conn: sqlite3.Connection,
    stable_ids: Sequence[str],
    *,
    machine_id: str | None = None,
) -> dict[str, list[str]]:
    """file_path values per stable_id, for bulk availability on THIS machine.

    Filtered by ``machine_id`` (default: local) because an unfiltered read
    would report a file that lives on another machine as present here, which
    is exactly the overcount ADR 08 consequence 1 warns about.
    """
    out: dict[str, list[str]] = {sid: [] for sid in stable_ids}
    if not stable_ids or not locations_table_ready(conn):
        return out
    owner = machine_id or _sync_stamp.local_machine_id(conn)
    for batch in _batched(stable_ids, ID_BIND_BATCH):
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT stable_id, file_path FROM track_locations "
            f"WHERE stable_id IN ({placeholders}) AND machine_id = ? "
            f"AND file_path IS NOT NULL",
            (*batch, owner),
        ).fetchall()
        for stable_id, file_path in rows:
            if file_path:
                out.setdefault(stable_id, []).append(str(file_path))
    return out


@dataclass(frozen=True)
class PlayableCandidate:
    """One path that could make a track playable on this machine."""

    path: str
    source: str


def playable_candidate_paths(
    conn: sqlite3.Connection,
    primary_by_sid: Mapping[str, str | None],
    *,
    machine_id: str | None = None,
) -> dict[str, list[PlayableCandidate]]:
    """THE paths that make a track playable on this machine (issue #3934).

    The playlist tree's ``available_count``, the row wire's
    ``file_availability`` and the deck-load audio route all ask "is one of
    these on disk here?" of this one list, in this order:

    1. the caller's primary: rekordbox's FolderPath when the track is mapped,
       else ``tracks.file_path`` (``source`` ``"primary"``);
    2. this machine's live local ``track_locations`` rows, available first
       (``source`` ``"location:<id>"``);
    3. ``tracks.file_path`` (``source`` ``"file_path"``).

    The surfaces differ only in HOW they answer (the persisted index with no
    stat for the tree, a budgeted stat for rows, an open probe for the deck),
    never in WHICH paths count. Duplicates and empty paths are dropped; a
    streaming URI survives only as the primary, where it means "streaming".
    """
    stable_ids = list(primary_by_sid)
    out: dict[str, list[PlayableCandidate]] = {sid: [] for sid in stable_ids}
    if not stable_ids:
        return out
    locations: dict[str, list[PlayableCandidate]] = {sid: [] for sid in stable_ids}
    if _locations_machine_scoped(conn):
        owner = machine_id or _sync_stamp.local_machine_id(conn)
        for batch in _batched(stable_ids, ID_BIND_BATCH):
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"SELECT stable_id, location_id, file_path FROM track_locations "
                f"WHERE stable_id IN ({placeholders}) AND machine_id = ? "
                f"AND deleted_at IS NULL AND kind = 'local' "
                f"AND file_path IS NOT NULL AND file_path != '' "
                f"ORDER BY stable_id, available DESC, "
                f"CASE role WHEN 'primary' THEN 0 ELSE 1 END, created_at, location_id",
                (*batch, owner),
            ).fetchall()
            for stable_id, location_id, file_path in rows:
                locations[str(stable_id)].append(
                    PlayableCandidate(str(file_path), f"location:{location_id}")
                )
    track_paths: dict[str, str] = {}
    tracks_deleted_filter = " AND deleted_at IS NULL" if _tracks_soft_deletes(conn) else ""
    for batch in _batched(stable_ids, ID_BIND_BATCH):
        placeholders = ",".join("?" * len(batch))
        for stable_id, file_path in conn.execute(
            f"SELECT stable_id, file_path FROM tracks "
            f"WHERE stable_id IN ({placeholders}){tracks_deleted_filter}",
            batch,
        ).fetchall():
            if file_path:
                track_paths[str(stable_id)] = str(file_path)
    for sid in stable_ids:
        primary = primary_by_sid[sid]
        ordered = [
            *([PlayableCandidate(primary, "primary")] if primary else []),
            *locations[sid],
            *(
                [PlayableCandidate(track_paths[sid], "file_path")]
                if sid in track_paths
                else []
            ),
        ]
        seen: set[str] = set()
        for candidate in ordered:
            if candidate.path in seen:
                continue
            if candidate.source != "primary" and candidate.path.startswith(
                platform_paths.STREAMING_PREFIXES
            ):
                continue
            seen.add(candidate.path)
            out[sid].append(candidate)
    return out


def upsert_location(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    kind: Kind,
    file_path: str | None = None,
    remote_url: str | None = None,
    role: Role = "alternate",
    content_hash: str | None = None,
    now: str | None = None,
    machine_id: str | None = None,
) -> str:
    """Insert or refresh one location for one machine. Returns location_id.

    The natural key is ``(stable_id, machine_id, kind, file_path|remote_url)``
    -- the same tuple the partial UNIQUE indexes assert -- so re-probing a
    file updates its row instead of minting a second ``location_id`` that the
    hub would reject with a UNIQUE-violation 409 (round 1 finding 1).
    Every write is stamped and appended to ``local_changelog``.

    ``file_path`` and ``remote_url`` are NFC-normalized before the lookup AND
    before the insert (:func:`normalize_stored_text`, round 2 finding N2), so
    two Unicode spellings of one path resolve to one row rather than two the
    hub will later collapse behind this machine's back.
    """
    if kind not in ("local", "remote"):
        raise LocationError(f"kind must be local or remote, got {kind!r}")
    if role not in ("primary", "alternate"):
        raise LocationError(f"role must be primary or alternate, got {role!r}")
    if not file_path and not remote_url:
        raise LocationError("location needs file_path or remote_url")
    file_path = normalize_stored_text(file_path)
    remote_url = normalize_stored_text(remote_url)
    owner = machine_id or _sync_stamp.ensure_local_machine(conn)
    venue_key, venue_rank, available = _probe_file(file_path)
    existing = _find_existing(conn, stable_id, owner, kind, file_path, remote_url)
    location_id = existing if existing is not None else uuid.uuid4().hex
    stamp = _sync_stamp.stamp_and_log(
        conn, "track_locations", (location_id,), owner, now=now,
    )
    if existing is None:
        conn.execute(
            "INSERT INTO track_locations("
            "location_id, stable_id, machine_id, kind, role, file_path, "
            "remote_url, venue_key, venue_rank, available, probed_at, "
            "content_hash, created_at, updated_at, origin_device_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                location_id,
                stable_id,
                owner,
                kind,
                role,
                file_path,
                remote_url,
                venue_key,
                venue_rank,
                1 if available else 0,
                stamp.updated_at,
                content_hash,
                stamp.updated_at,
                stamp.updated_at,
                stamp.origin_device_id,
            ),
        )
        return location_id
    conn.execute(
        "UPDATE track_locations SET role=?, venue_key=?, venue_rank=?, "
        "available=?, probed_at=?, content_hash=?, updated_at=?, "
        "origin_device_id=? WHERE location_id=?",
        (
            role,
            venue_key,
            venue_rank,
            1 if available else 0,
            stamp.updated_at,
            content_hash,
            stamp.updated_at,
            stamp.origin_device_id,
            location_id,
        ),
    )
    return location_id


def should_attach_local_primary(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    file_path: str | None,
    machine_id: str,
    wrote_path_here: bool,
) -> bool:
    """Whether this machine may own a local primary for ``file_path``.

    A cloud-synced ``tracks.file_path`` was written on the origin machine.
    Stamping it as a local primary here makes availability count another
    machine's disk (schema v6 / ADR 08). Attach only when this write
    inserted or changed the path, the path resolves on this machine, or
    this machine already has that location and is refreshing it.
    """
    if not file_path or not locations_table_ready(conn):
        return False
    if wrote_path_here:
        return True
    _venue_key, _venue_rank, available = _probe_file(file_path)
    if available:
        return True
    return (
        _find_existing(conn, stable_id, machine_id, "local", file_path, None)
        is not None
    )


def sync_primary_local(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    file_path: str | None,
    now: str | None = None,
    machine_id: str | None = None,
) -> None:
    """Keep the ingest primary row aligned with ``tracks.file_path``."""
    if not file_path or not locations_table_ready(conn):
        return
    upsert_location(
        conn,
        stable_id=stable_id,
        kind="local",
        file_path=file_path,
        role="primary",
        now=now,
        machine_id=machine_id,
    )


def pick_playable(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    policy: PickPolicy | None = None,
    folder_path: str | None = None,
    duration_ms: int | None = None,
    extra_paths: Sequence[tuple[str, str, Kind]] = (),
) -> PickedAudio | None:
    """Return the single playable winner, or None when nothing works."""
    policy = policy or policy_from_env()
    duration = duration_ms
    if duration is None:
        row = conn.execute(
            "SELECT duration_ms, file_path FROM tracks WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
        if row is None:
            return None
        duration = row[0]
        implicit_file = row[1]
    else:
        implicit_file = conn.execute(
            "SELECT file_path FROM tracks WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
        implicit_file = implicit_file[0] if implicit_file else None

    seen: set[str] = set()
    candidates: list[_Candidate] = []
    for loc in list_locations(conn, stable_id):
        cand = _candidate_from_path(
            loc.file_path, kind=loc.kind, source=f"location:{loc.location_id}",
            duration_ms=duration,
        )
        if cand is not None:
            seen.add(str(cand.path))
            candidates.append(cand)
    implicit_paths: tuple[tuple[str | None, str, Kind], ...] = (
        (implicit_file, "file_path", "local"),
        (folder_path, "folder_path", "local"),
    )
    for raw, source, kind in (*implicit_paths, *extra_paths):
        cand = _candidate_from_path(
            raw, kind=kind, source=source, duration_ms=duration,
        )
        if cand is not None and str(cand.path) not in seen:
            seen.add(str(cand.path))
            candidates.append(cand)

    return _pick_winner(candidates, policy)


def pick_playable_from_candidates(
    conn: sqlite3.Connection,
    stable_id: str,
    paths: Sequence[PlayableCandidate],
    *,
    policy: PickPolicy | None = None,
) -> PickedAudio | None:
    """The playable winner among exactly ``paths``, the
    :func:`playable_candidate_paths` list the listing counts (issue #3934)."""
    policy = policy or policy_from_env()
    row = conn.execute(
        "SELECT duration_ms FROM tracks WHERE stable_id = ?", (stable_id,)
    ).fetchone()
    duration = row[0] if row is not None else None
    candidates = [
        cand
        for candidate in paths
        if (
            cand := _candidate_from_path(
                candidate.path, kind="local", source=candidate.source, duration_ms=duration
            )
        )
        is not None
    ]
    return _pick_winner(candidates, policy)


def _pick_winner(candidates: Sequence[_Candidate], policy: PickPolicy) -> PickedAudio | None:
    """Pick order 1-4 from the module docstring over built candidates."""
    working = [c for c in candidates if c.working]
    if not working:
        return None
    pool = working
    if policy.prefer_local:
        locals_only = [c for c in working if c.kind == "local"]
        if locals_only:
            pool = locals_only
    windowed = [
        c for c in pool
        if c.venue_rank is not None
        and policy.min_venue_rank <= c.venue_rank <= policy.max_venue_rank
    ]
    chosen_pool = windowed or pool
    winner = max(
        chosen_pool,
        key=lambda c: (c.venue_rank if c.venue_rank is not None else -1),
    )
    return PickedAudio(
        path=winner.path,
        media_type=winner.media_type,
        kind=winner.kind,
        venue_key=winner.venue_key,
        venue_rank=winner.venue_rank,
        source=winner.source,
    )


def _find_existing(
    conn: sqlite3.Connection,
    stable_id: str,
    machine_id: str,
    kind: str,
    file_path: str | None,
    remote_url: str | None,
) -> str | None:
    """The ``location_id`` already holding this natural key, if any."""
    if file_path:
        row = conn.execute(
            "SELECT location_id FROM track_locations "
            "WHERE stable_id=? AND machine_id=? AND kind=? AND file_path=?",
            (stable_id, machine_id, kind, file_path),
        ).fetchone()
        return str(row[0]) if row else None
    row = conn.execute(
        "SELECT location_id FROM track_locations "
        "WHERE stable_id=? AND machine_id=? AND kind=? AND remote_url=?",
        (stable_id, machine_id, kind, remote_url),
    ).fetchone()
    return str(row[0]) if row else None


def _probe_file(file_path: str | None) -> tuple[str | None, int | None, bool]:
    if not file_path:
        return None, None, False
    mapped = platform_paths.resolve_asset_path(file_path)
    if mapped.resolved is None or not fs_residency.is_materialised(mapped.resolved):
        return None, None, False
    quality = audio_quality.classify(str(mapped.resolved), None)
    if quality.venue is None:
        return None, None, True
    return quality.venue.key, quality.venue.rank, True


def _candidate_from_path(
    raw: str | None,
    *,
    kind: Kind,
    source: str,
    duration_ms: int | None,
) -> _Candidate | None:
    if not raw or raw.startswith(platform_paths.STREAMING_PREFIXES):
        return None
    mapped = platform_paths.resolve_asset_path(raw)
    if mapped.resolved is None:
        return None
    path = mapped.resolved
    media_type = AUDIO_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        return None
    working = fs_residency.exists_for_audio_open_probe(path)
    size = fs_residency.materialised_size(path) if fs_residency.is_materialised(path) else None
    quality = audio_quality.classify(str(path), duration_ms, size)
    return _Candidate(
        kind=kind,
        path=path,
        media_type=media_type,
        venue_key=quality.venue.key if quality.venue else None,
        venue_rank=quality.venue.rank if quality.venue else None,
        source=source,
        working=working,
    )


def _row_to_location(row: tuple[object, ...]) -> TrackLocation:
    if row[1] is None:
        raise LocationError(
            f"track_locations row {row[0]!r} has no machine_id; migration v6 "
            f"backfills it through apps.shared.state.db.open_rw, so this DB "
            f"was migrated by a path that skipped the hook."
        )
    return TrackLocation(
        location_id=str(row[0]),
        machine_id=str(row[1]),
        stable_id=str(row[2]),
        kind=row[3],  # type: ignore[arg-type]
        role=row[4],  # type: ignore[arg-type]
        file_path=str(row[5]) if row[5] else None,
        remote_url=str(row[6]) if row[6] else None,
        venue_key=str(row[7]) if row[7] else None,
        venue_rank=int(row[8]) if row[8] is not None else None,
        available=bool(row[9]),
        probed_at=str(row[10]) if row[10] else None,
        content_hash=str(row[11]) if row[11] else None,
    )
