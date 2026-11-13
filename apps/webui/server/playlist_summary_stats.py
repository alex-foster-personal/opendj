"""Aggregate playlist membership stats without hydrating full track rows (SETPREP-10645)."""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from pathlib import Path

from apps.adapters.rekordbox.paths import is_streaming_path
from apps.analysis.selection import Selection, field_column_sql

from .backend import NotFoundError, Track
from .rb_vendor_pkg.track_rows import bulk_availability
from .track_features import energy_of


@dataclass(frozen=True)
class PlaylistSummaryStatsData:
    tracks: int
    duration_s: float
    bpm_min: float | None
    bpm_median: float | None
    bpm_max: float | None
    bpm_missing: int
    energy_min: int | None
    energy_max: int | None
    energy_missing: int
    keys_distinct: int
    keys_missing: int
    unanalysed: int
    #: LIBM-105b: members whose local audio is not on this machine (missing file, unmounted volume, no track row).
    #: Streaming and pathless members have no local file to lose and are not counted; nor is an unchecked row.
    unavailable: int


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _file_present_for_duration(
    track: Track | None,
    availability: str | None,
) -> bool:
    if track is None:
        return False
    if availability == "present":
        return True
    path = track.file_path
    if not path or is_streaming_path(path):
        return False
    return Path(path).is_file()


def _has_local_audio(track: Track | None) -> bool:
    """True when the member should have a local file: a dangling membership or a non-streaming path."""
    if track is None:
        return True
    return bool(track.file_path) and not is_streaming_path(track.file_path)


def stats_from_distinct_members(
    member_ids: list[str],
    tracks_by_id: dict[str, Track],
    availability: dict[str, str] | None = None,
) -> PlaylistSummaryStatsData:
    """Derive summary stats from deduped membership stable_ids."""
    distinct = list(dict.fromkeys(member_ids))
    tracks_n = len(distinct)
    if tracks_n == 0:
        return PlaylistSummaryStatsData(
            tracks=0,
            duration_s=0.0,
            bpm_min=None,
            bpm_median=None,
            bpm_max=None,
            bpm_missing=0,
            energy_min=None,
            energy_max=None,
            energy_missing=0,
            keys_distinct=0,
            keys_missing=0,
            unanalysed=0,
            unavailable=0,
        )

    duration_s = 0.0
    bpms: list[float] = []
    bpm_missing = 0
    energies: list[int] = []
    energy_missing = 0
    keys: set[str] = set()
    keys_missing = 0
    unanalysed = 0
    unavailable = 0
    avail = availability or {}

    for sid in distinct:
        track = tracks_by_id.get(sid)
        present = _file_present_for_duration(track, avail.get(sid))
        if not present and _has_local_audio(track):
            unavailable += 1

        if track is not None and track.duration_ms is not None:
            duration_s += track.duration_ms / 1000.0
        elif present:
            duration_s += 0.0

        bpm = track.bpm if track is not None else None
        key = (track.key or "").strip() if track is not None and track.key else ""
        if bpm is None:
            bpm_missing += 1
        else:
            bpms.append(float(bpm))
        energy = energy_of(track) if track is not None else None
        if energy is None:
            energy_missing += 1
        else:
            energies.append(energy)
        if not key:
            keys_missing += 1
        else:
            keys.add(key)
        if bpm is None or not key:
            unanalysed += 1

    return PlaylistSummaryStatsData(
        tracks=tracks_n,
        duration_s=duration_s,
        bpm_min=min(bpms) if bpms else None,
        bpm_median=_median(bpms),
        bpm_max=max(bpms) if bpms else None,
        bpm_missing=bpm_missing,
        energy_min=min(energies) if energies else None,
        energy_max=max(energies) if energies else None,
        energy_missing=energy_missing,
        keys_distinct=len(keys),
        keys_missing=keys_missing,
        unanalysed=unanalysed,
        unavailable=unavailable,
    )


def stats_from_in_memory(
    member_ids: list[str],
    tracks_by_id: dict[str, Track],
    file_paths: dict[str, str | None],
    *,
    state_db_path: Path | None = None,
) -> PlaylistSummaryStatsData:
    distinct = list(dict.fromkeys(member_ids))
    availability = bulk_availability(
        distinct,
        file_paths,
        state_db_path=state_db_path,
    )
    avail_names = {sid: ("present" if ok else "absent") for sid, ok in availability.items()}
    return stats_from_distinct_members(member_ids, tracks_by_id, avail_names)


#: track_availability states that mean the audio is expected here but not readable (schema AVAILABILITY_STATES).
_UNAVAILABLE_STATES: frozenset[str] = frozenset({"absent", "awaiting_volume"})


def _stats_from_sqlite_member_rows(
    rows: list[sqlite3.Row],
) -> PlaylistSummaryStatsData:
    if not rows:
        return PlaylistSummaryStatsData(
            tracks=0,
            duration_s=0.0,
            bpm_min=None,
            bpm_median=None,
            bpm_max=None,
            bpm_missing=0,
            energy_min=None,
            energy_max=None,
            energy_missing=0,
            keys_distinct=0,
            keys_missing=0,
            unanalysed=0,
            unavailable=0,
        )

    duration_s = 0.0
    bpms: list[float] = []
    bpm_missing = 0
    energies: list[int] = []
    energy_missing = 0
    keys: set[str] = set()
    keys_missing = 0
    unanalysed = 0
    unavailable = 0

    for row in rows:
        has_track = bool(row["has_track"])
        duration_ms = row["duration_ms"]
        avail_state = row["avail_state"] or ""
        bpm = row["bpm"]
        key_raw = row["track_key"]
        key = (key_raw or "").strip() if key_raw is not None else ""
        energy = row["energy"]

        if duration_ms is not None:
            duration_s += float(duration_ms) / 1000.0
        elif avail_state == "present":
            duration_s += 0.0

        bpm_missing_row = not has_track or bpm is None
        key_missing_row = not has_track or not key
        if bpm_missing_row:
            bpm_missing += 1
        else:
            bpms.append(float(bpm))
        if energy is None:
            energy_missing += 1
        else:
            energies.append(int(energy))
        if key_missing_row:
            keys_missing += 1
        else:
            keys.add(key)
        if bpm_missing_row or key_missing_row:
            unanalysed += 1
        if not has_track or avail_state in _UNAVAILABLE_STATES:
            unavailable += 1

    return PlaylistSummaryStatsData(
        tracks=len(rows),
        duration_s=duration_s,
        bpm_min=min(bpms) if bpms else None,
        bpm_median=_median(bpms),
        bpm_max=max(bpms) if bpms else None,
        bpm_missing=bpm_missing,
        energy_min=min(energies) if energies else None,
        energy_max=max(energies) if energies else None,
        energy_missing=energy_missing,
        keys_distinct=len(keys),
        keys_missing=keys_missing,
        unanalysed=unanalysed,
        unavailable=unavailable,
    )


def stats_from_sqlite(conn: sqlite3.Connection, playlist_id: str) -> PlaylistSummaryStatsData:
    """One aggregate statement for a stored playlist; raises NotFoundError when unknown."""
    row = conn.execute(
        "SELECT 1 FROM playlists WHERE playlist_id = ? AND deleted_at IS NULL",
        (playlist_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"playlist not found: {playlist_id}")

    selection = Selection.resolve(conn)
    bpm = field_column_sql("bpm", selection, table="d")
    key = field_column_sql("key", selection, table="d")
    energy = (
        "(SELECT CAST(json_extract(tf.value_json, '$') AS INTEGER) "
        "FROM track_fields tf "
        "WHERE tf.stable_id = d.stable_id AND tf.field_name = 'energy' LIMIT 1)"
    )
    sql = f"""
    WITH deduped AS (
        SELECT DISTINCT m.stable_id AS stable_id
        FROM playlist_memberships m
        WHERE m.playlist_id = ? AND m.deleted_at IS NULL
    )
    SELECT
        d.stable_id,
        t.stable_id IS NOT NULL AS has_track,
        t.duration_ms,
        COALESCE(ta.state, '') AS avail_state,
        CAST({bpm} AS REAL) AS bpm,
        TRIM(CAST({key} AS TEXT)) AS track_key,
        {energy} AS energy
    FROM deduped d
    LEFT JOIN tracks t ON t.stable_id = d.stable_id AND t.deleted_at IS NULL
    LEFT JOIN track_availability ta ON ta.stable_id = d.stable_id
    """
    prior_row_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (playlist_id,)).fetchall()
    finally:
        conn.row_factory = prior_row_factory
    return _stats_from_sqlite_member_rows(rows)
