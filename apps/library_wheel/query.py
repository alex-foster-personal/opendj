"""Deterministic aggregation for the LIBUX-06 Library Wheel.

Requirements:
✔︎ Group the real library by genre family (state.db + master.plain.db), never demo nodes.
✔︎ Unmapped tracks use state-layer ``track_fields`` genre (STANDALONE-05); rekordbox
  remains authoritative when a live vendor mapping exists.
✔︎ Selectable axes overlay a per-track numeric value on the same genre tree.
✔︎ Two axes have no backing data yet (overplayed-ness, set played in) per
  REQUIREMENTS.md's own callout, and a third was found missing during build
  (decade/release-year is not ingested anywhere in this repo) -- all three
  ship disabled with a stated reason rather than faked, per the house
  brittle-fail-fast rule.

Acceptance tests:
[if] a folder-imported file carries a GENRE tag and no rekordbox is present [then
     ⛔️] it counts as unclassified instead of joining a genre family
[if] a disabled axis is requested [then ⛔️] any track carries a non-null axis_value
[if] state.db is missing [then ⛔️] an empty success payload is returned instead of raising
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .genre_families import simple_genre_family

_SQL_CHUNK = 500


@dataclass(frozen=True)
class AxisInfo:
    key: str
    label: str
    enabled: bool
    reason: str | None


AXES: tuple[AxisInfo, ...] = (
    AxisInfo("genre", "Genre", True, None),
    AxisInfo(
        "decade",
        "Decade",
        False,
        "release year is not ingested into state.db or master.plain.db by this "
        "repo yet -- no track carries one (found during LIBUX-06 build; not one "
        "of the two prerequisite gaps the requirement itself named)",
    ),
    AxisInfo("play_count", "Play count", True, None),
    AxisInfo("popularity", "Popularity within genre", True, None),
    AxisInfo(
        "overplayed_ness",
        "Overplayed-ness",
        False,
        "needs a popularity-curve calculation that LIBUX-06 marks as an "
        "undefined prerequisite",
    ),
    AxisInfo("playlist", "Playlist membership", True, None),
    AxisInfo(
        "set_played_in",
        "Set played in",
        False,
        "needs SET-08 set-history data and the Open DJ deck observer, "
        "neither built yet (LIBUX-06 prerequisite)",
    ),
)
_AXES_BY_KEY: dict[str, AxisInfo] = {axis.key: axis for axis in AXES}


class LibraryWheelError(RuntimeError):
    """The configured library cannot satisfy the wheel's data contract."""


def _open_readonly(db_path: Path, label: str) -> sqlite3.Connection:
    resolved = db_path.resolve()
    if not resolved.is_file():
        raise LibraryWheelError(f"{label} does not exist: {resolved}")
    try:
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        raise LibraryWheelError(f"cannot open {label} read-only: {exc}") from exc
    else:
        return connection


def _chunked(seq: list[str], size: int = _SQL_CHUNK) -> list[list[str]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _decode_field_json(value_json: str | None) -> str | None:
    if not value_json:
        return None
    try:
        decoded = json.loads(value_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(decoded, str):
        stripped = decoded.strip()
        return stripped or None
    return None


def _artist_of(artists_json: str | None) -> str | None:
    if not artists_json:
        return None
    try:
        decoded = json.loads(artists_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(decoded, list):
        joined = ", ".join(str(a) for a in decoded if a)
        return joined or None
    if isinstance(decoded, str):
        return decoded or None
    return None


def _load_tracks(state: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = state.execute(
        "SELECT stable_id, title, artists_json FROM tracks WHERE deleted_at IS NULL"
    ).fetchall()
    return {
        str(row["stable_id"]): {
            "stable_id": str(row["stable_id"]),
            "title": row["title"],
            "artist": _artist_of(row["artists_json"]),
        }
        for row in rows
    }


def _load_vendor_ids(state: sqlite3.Connection) -> dict[str, str]:
    rows = state.execute(
        "SELECT stable_id, vendor_id FROM track_vendor_ids "
        "WHERE vendor = 'rekordbox' AND deleted_at IS NULL"
    ).fetchall()
    return {str(row["stable_id"]): str(row["vendor_id"]) for row in rows}


def _load_local_genres(state: sqlite3.Connection) -> dict[str, str]:
    rows = state.execute(
        "SELECT stable_id, value_json FROM track_fields "
        "WHERE field_name = 'genre' AND deleted_at IS NULL"
    ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        genre = _decode_field_json(row["value_json"])
        if genre is not None:
            out[str(row["stable_id"])] = genre
    return out


def _load_playlist_counts(state: sqlite3.Connection) -> dict[str, int]:
    rows = state.execute(
        "SELECT m.stable_id AS stable_id, COUNT(DISTINCT m.playlist_id) AS n "
        "FROM playlist_memberships m "
        "JOIN playlists p ON p.playlist_id = m.playlist_id "
        "WHERE m.deleted_at IS NULL AND p.deleted_at IS NULL "
        "GROUP BY m.stable_id"
    ).fetchall()
    return {str(row["stable_id"]): int(row["n"]) for row in rows}


def _load_genre_and_play_count(
    master: sqlite3.Connection, vendor_ids: list[str]
) -> dict[str, tuple[str | None, int]]:
    """vendor_id -> (raw genre tag, DJPlayCount). Same join bulk_rb_meta uses,
    including the unary + that keeps the ID key in the plan (LIBM-128)."""
    out: dict[str, tuple[str | None, int]] = {}
    for chunk in _chunked(sorted(set(vendor_ids))):
        placeholders = ",".join("?" * len(chunk))
        rows = master.execute(
            f"""
            SELECT c.ID AS vendor_id, g.Name AS genre, c.DJPlayCount AS play_count
            FROM djmdContent c
            LEFT JOIN djmdGenre g ON g.ID = c.GenreID AND g.rb_local_deleted = 0
            WHERE c.ID IN ({placeholders}) AND +c.rb_local_deleted = 0
            """,
            chunk,
        ).fetchall()
        for row in rows:
            out[str(row["vendor_id"])] = (row["genre"], int(row["play_count"] or 0))
    return out


@dataclass(frozen=True)
class _LoadedLibrary:
    tracks: dict[str, dict[str, Any]]
    vendor_id_by_stable_id: dict[str, str]
    playlist_count_by_stable_id: dict[str, int]
    genre_and_plays: dict[str, tuple[str | None, int]]
    local_genre_by_stable_id: dict[str, str]


def _load_library(state_db: Path, master_db: Path) -> _LoadedLibrary:
    state = _open_readonly(state_db, "state.db")
    try:
        tracks = _load_tracks(state)
        vendor_id_by_stable_id = _load_vendor_ids(state)
        playlist_count_by_stable_id = _load_playlist_counts(state)
        local_genre_by_stable_id = _load_local_genres(state)
    except sqlite3.Error as exc:
        raise LibraryWheelError(f"state.db query failed: {exc}") from exc
    finally:
        state.close()

    genre_and_plays: dict[str, tuple[str | None, int]] = {}
    if vendor_id_by_stable_id:
        master = _open_readonly(master_db, "master.plain.db")
        try:
            genre_and_plays = _load_genre_and_play_count(
                master, list(vendor_id_by_stable_id.values())
            )
        except sqlite3.Error as exc:
            raise LibraryWheelError(f"master.plain.db query failed: {exc}") from exc
        finally:
            master.close()

    return _LoadedLibrary(
        tracks=tracks,
        vendor_id_by_stable_id=vendor_id_by_stable_id,
        playlist_count_by_stable_id=playlist_count_by_stable_id,
        genre_and_plays=genre_and_plays,
        local_genre_by_stable_id=local_genre_by_stable_id,
    )


@dataclass(frozen=True)
class _GenreTree:
    # family -> genre tag -> list of track dicts (pre-axis).
    rows_by_family_and_genre: dict[str, dict[str, list[dict[str, Any]]]]
    family_colors: dict[str, str]
    family_play_counts: dict[str, dict[str, int]]
    unclassified_count: int


def _build_genre_tree(library: _LoadedLibrary) -> _GenreTree:
    tree: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    family_colors: dict[str, str] = {}
    family_play_counts: dict[str, dict[str, int]] = defaultdict(dict)
    unclassified_count = 0

    for stable_id, track in library.tracks.items():
        vendor_id = library.vendor_id_by_stable_id.get(stable_id)
        content = library.genre_and_plays.get(vendor_id) if vendor_id is not None else None
        if content is not None:
            # Live rekordbox content wins over any local track_fields genre.
            genre_tag, play_count = content
            play_count_source = "rekordbox"
        else:
            # No mapping, or a mapping whose djmdContent row is gone/deleted:
            # bulk_rb_meta treats that as unmapped, so the wheel does too.
            genre_tag = library.local_genre_by_stable_id.get(stable_id)
            play_count = 0
            play_count_source = "local"
        family = simple_genre_family(genre_tag)
        if genre_tag is None or family is None:
            unclassified_count += 1
            continue
        family_name, color = family
        family_colors[family_name] = color
        family_play_counts[family_name][stable_id] = play_count
        tree[family_name][genre_tag].append(
            {
                **track,
                "genre": genre_tag,
                "play_count": play_count,
                "play_count_source": play_count_source,
                "playlist_count": library.playlist_count_by_stable_id.get(stable_id, 0),
            }
        )

    return _GenreTree(
        rows_by_family_and_genre=tree,
        family_colors=family_colors,
        family_play_counts=family_play_counts,
        unclassified_count=unclassified_count,
    )


def _popularity_percentiles(
    family_play_counts: dict[str, dict[str, int]],
) -> dict[str, dict[str, float]]:
    """Percentile rank of play_count, scoped to tracks resolved to that SAME
    family (the only honest denominator -- a track outside it never had a
    chance to rank here)."""
    percentile_by_family_and_track: dict[str, dict[str, float]] = {}
    for family_name, plays_by_id in family_play_counts.items():
        ordered = sorted(plays_by_id.items(), key=lambda kv: (kv[1], kv[0]))
        n = len(ordered)
        positions_by_play_count: dict[int, list[int]] = defaultdict(list)
        for position, (_stable_id, play_count) in enumerate(ordered):
            positions_by_play_count[play_count].append(position)
        percentile_by_play_count = {
            play_count: 100.0
            if n == 1
            else round(100.0 * (positions[0] + positions[-1]) / 2 / (n - 1), 1)
            for play_count, positions in positions_by_play_count.items()
        }
        ranks = {
            stable_id: percentile_by_play_count[play_count]
            for stable_id, play_count in ordered
        }
        percentile_by_family_and_track[family_name] = ranks
    return percentile_by_family_and_track


def _render_track(
    row: dict[str, Any],
    *,
    selected: AxisInfo,
    family_name: str,
    family_size: int,
    percentile: float | None,
) -> dict[str, Any]:
    axis_value, axis_title = _axis_value_and_title(
        selected, row, family_name=family_name, family_size=family_size, percentile=percentile
    )
    return {
        "stable_id": row["stable_id"],
        "title": row["title"],
        "artist": row["artist"],
        "genre": row["genre"],
        "axis_value": axis_value,
        "axis_title": axis_title,
    }


def _render_families(tree: _GenreTree, selected: AxisInfo) -> list[dict[str, Any]]:
    percentiles = _popularity_percentiles(tree.family_play_counts)
    families_out: list[dict[str, Any]] = []
    for family_name in sorted(tree.rows_by_family_and_genre):
        rows_by_genre = tree.rows_by_family_and_genre[family_name]
        family_size = len(tree.family_play_counts[family_name])
        genres_out = [
            {
                "tag": genre_tag,
                "track_count": len(rows),
                "tracks": [
                    _render_track(
                        row,
                        selected=selected,
                        family_name=family_name,
                        family_size=family_size,
                        percentile=percentiles[family_name].get(row["stable_id"]),
                    )
                    for row in rows
                ],
            }
            for genre_tag, rows in sorted(rows_by_genre.items())
        ]
        families_out.append(
            {
                "name": family_name,
                "color": tree.family_colors[family_name],
                "track_count": sum(g["track_count"] for g in genres_out),
                "genres": genres_out,
            }
        )
    return families_out


def query_library_wheel(
    state_db: Path, master_db: Path, *, axis: str = "play_count"
) -> dict[str, Any]:
    """One deterministic, JSON-ready genre-wheel read model.

    ``axis`` selects the per-track numeric overlay (see :data:`AXES`).
    Requesting a disabled axis still returns the real genre tree; every
    track's ``axis_value``/``axis_title`` is null and the reason is named
    at both the top level and in that axis's own ``axes`` entry -- never a
    fabricated number.
    """
    if axis not in _AXES_BY_KEY:
        raise ValueError(f"unsupported axis: {axis!r}")
    selected = _AXES_BY_KEY[axis]

    library = _load_library(state_db, master_db)
    tree = _build_genre_tree(library)

    return {
        "schema_version": 1,
        "axis": axis,
        "axes": [
            {"key": a.key, "label": a.label, "enabled": a.enabled, "reason": a.reason}
            for a in AXES
        ],
        "selected_axis_enabled": selected.enabled,
        "selected_axis_reason": selected.reason,
        "total_tracks": len(library.tracks),
        "unclassified_track_count": tree.unclassified_count,
        "families": _render_families(tree, selected),
    }


def _axis_value_and_title(
    axis: AxisInfo,
    row: dict[str, Any],
    *,
    family_name: str,
    family_size: int,
    percentile: float | None,
) -> tuple[float | int | None, str | None]:
    if not axis.enabled:
        return None, None
    if axis.key == "genre":
        return None, None
    if axis.key == "play_count":
        value = row["play_count"]
        if row.get("play_count_source") == "rekordbox":
            return value, f"{value} plays (rekordbox DJPlayCount)"
        return value, f"{value} plays (local, no rekordbox mapping)"
    if axis.key == "popularity":
        assert percentile is not None
        return (
            percentile,
            f"{percentile:g}th percentile of play count within {family_name} "
            f"({family_size} tracks with a resolved genre family)",
        )
    if axis.key == "playlist":
        value = row["playlist_count"]
        return value, f"in {value} playlist(s)"
    raise AssertionError(f"unhandled enabled axis: {axis.key}")


__all__ = ["AXES", "AxisInfo", "LibraryWheelError", "query_library_wheel"]
