"""Read a Mixxx ``mixxxdb.sqlite`` into an ``OpenDjLibrary`` (DJIMPORT-10059).

Tracks, cues, playlists, crates, the key table, and drop rules for hidden or
deleted entries. Fields and cues ingest with the shared ``open-dj-tool``
provenance label; a dedicated ``mixxx`` label needs a state schema migration.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from apps.open_dj import CuePoint, OpenDjLibrary, Playlist, Track
from apps.open_dj.provenance import utc_mtime

_REQUIRED_TABLES = (
    "library",
    "track_locations",
    "cues",
    "Playlists",
    "PlaylistTracks",
    "crates",
    "crate_tracks",
)

DEFAULT_LIBRARY_CANDIDATES: tuple[PurePosixPath, ...] = (
    PurePosixPath("Library/Containers/org.mixxx.mixxx/Data/Library/Application Support/Mixxx/mixxxdb.sqlite"),
    PurePosixPath("Library/Application Support/Mixxx/mixxxdb.sqlite"),
)

#: One row per key: (Camelot = Lancelot, Open Key, traditional spellings).
MIXXX_KEYS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("1A", "6m", ("Abm", "G#m")),
    ("1B", "6d", ("B", "Cb")),
    ("2A", "7m", ("Ebm", "D#m")),
    ("2B", "7d", ("F#", "Gb")),
    ("3A", "8m", ("Bbm", "A#m")),
    ("3B", "8d", ("Db", "C#")),
    ("4A", "9m", ("Fm",)),
    ("4B", "9d", ("Ab", "G#")),
    ("5A", "10m", ("Cm",)),
    ("5B", "10d", ("Eb", "D#")),
    ("6A", "11m", ("Gm",)),
    ("6B", "11d", ("Bb", "A#")),
    ("7A", "12m", ("Dm",)),
    ("7B", "12d", ("F",)),
    ("8A", "1m", ("Am",)),
    ("8B", "1d", ("C",)),
    ("9A", "2m", ("Em",)),
    ("9B", "2d", ("G",)),
    ("10A", "3m", ("Bm",)),
    ("10B", "3d", ("D",)),
    ("11A", "4m", ("F#m", "Gbm")),
    ("11B", "4d", ("A",)),
    ("12A", "5m", ("C#m", "Dbm")),
    ("12B", "5d", ("E",)),
)

_KEY_LOOKUP: dict[str, str] = {}
for _camelot, _open_key, _trad in MIXXX_KEYS:
    _KEY_LOOKUP[_camelot.upper()] = _camelot
    _KEY_LOOKUP[_open_key] = _camelot
    _KEY_LOOKUP[_open_key.upper()] = _camelot
    for _t in _trad:
        _KEY_LOOKUP[_t] = _camelot


def mixxx_key_to_camelot(text: str | None) -> str | None:
    """Map Mixxx's key column (Lancelot, Open Key or traditional) to Camelot."""
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    normalized = stripped.replace("♯", "#").replace("♭", "b")
    token = normalized.split()[0]
    if token in _KEY_LOOKUP:
        return _KEY_LOOKUP[token]
    upper = token.upper()
    if upper in _KEY_LOOKUP:
        return _KEY_LOOKUP[upper]
    if len(token) >= 2 and token[-1] in "mMdD":
        lowered = f"{token[:-1]}{token[-1].lower()}"
        return _KEY_LOOKUP.get(lowered)
    return None


@dataclass(frozen=True)
class MixxxLibrary:
    library: OpenDjLibrary
    #: Tracks whose file Mixxx marked deleted (track_locations.fs_deleted = 1), not imported.
    tracks_fs_deleted: int = 0
    #: Tracks hidden in Mixxx (library.mixxx_deleted = 1), not imported.
    tracks_hidden: int = 0
    #: Every other loss, counted per kind as "<field> <action>: <reason>".
    notes: dict[str, int] = field(default_factory=dict)


def read_mixxx_library(db_path: Path) -> MixxxLibrary:
    """Assemble ``db_path`` (Mixxx's ``mixxxdb.sqlite``) into an ``OpenDjLibrary``."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"Mixxx library not found at {db_path}")
    stamp = utc_mtime(db_path)
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return _read(conn, db_path=db_path, source_modified_at=stamp)
    finally:
        conn.close()


def _read(conn: sqlite3.Connection, *, db_path: Path, source_modified_at: datetime) -> MixxxLibrary:
    _guard_schema(conn, db_path)
    cols = _column_flags(conn)
    notes: dict[str, int] = defaultdict(int)
    tracks, kept, samplerates, tracks_fs_deleted, tracks_hidden = _load_tracks(conn, cols, source_modified_at, notes)
    updated_tracks = _attach_cues(conn, cols, tracks, samplerates, notes)
    playlists = list(_read_playlists(conn, kept, notes))
    crates = _read_crates(conn, kept, notes)
    if crates:
        playlists.append(Playlist(name="Crates", track_ids=(), children=tuple(crates)))

    return MixxxLibrary(
        library=OpenDjLibrary(version="0.1", tracks=tuple(updated_tracks), playlists=tuple(playlists)),
        tracks_fs_deleted=tracks_fs_deleted,
        tracks_hidden=tracks_hidden,
        notes=dict(notes),
    )


def _load_tracks(
    conn: sqlite3.Connection,
    cols: dict[str, set[str]],
    source_modified_at: datetime,
    notes: dict[str, int],
) -> tuple[list[Track], set[str], dict[str, int], int, int]:
    kept: set[str] = set()
    samplerates: dict[str, int] = {}
    tracks: list[Track] = []
    tracks_fs_deleted = 0
    tracks_hidden = 0
    for row in _fetch_tracks(conn, cols):
        tid, track, sr, deleted_kind = _track_from_row(row, source_modified_at, notes)
        if deleted_kind == "hidden":
            tracks_hidden += 1
        elif deleted_kind == "fs_deleted":
            tracks_fs_deleted += 1
        elif track is not None:
            samplerates[tid] = sr
            kept.add(tid)
            tracks.append(track)
    return tracks, kept, samplerates, tracks_fs_deleted, tracks_hidden


def _track_from_row(
    row: tuple,
    source_modified_at: datetime,
    notes: dict[str, int],
) -> tuple[str, Track | None, int, str | None]:
    (
        lid,
        title,
        artist,
        album,
        genre,
        year,
        bpm,
        key,
        rating,
        duration,
        timesplayed,
        color,
        samplerate,
        comment,
        location_path,
        fs_deleted,
        mixxx_deleted,
    ) = row
    tid = str(lid)
    if mixxx_deleted:
        return tid, None, 0, "hidden"
    if fs_deleted:
        return tid, None, 0, "fs_deleted"
    if not location_path or not str(location_path).strip():
        notes["location dropped: no file path in Mixxx"] += 1
        return tid, None, 0, "no_path"

    artists = (str(artist).strip(),) if artist and str(artist).strip() else ()
    bpm_val = _parse_bpm(bpm)
    key_raw = str(key).strip() if key else ""
    key_camelot = mixxx_key_to_camelot(key_raw) if key_raw else None
    if key_raw and key_camelot is None:
        notes["key dropped: notation not recognised"] += 1
    rating_val = _parse_rating(rating, notes)
    duration_ms = _parse_duration_ms(duration)
    play_count = int(timesplayed or 0)
    color_rgb = _parse_rgb(color)
    extensions = _track_extensions(comment, genre, year)
    sr = int(samplerate) if samplerate is not None else 0
    track = Track(
        track_id=tid,
        file_path=str(location_path),
        title=str(title or ""),
        artists=artists,
        album=str(album or ""),
        bpm=bpm_val,
        key_camelot=key_camelot,
        rating=rating_val,
        duration_ms=duration_ms,
        play_count=play_count,
        color_rgb=color_rgb,
        extensions=extensions,
        cues=(),
        source_modified_at=source_modified_at,
    )
    return tid, track, sr, None


def _parse_bpm(bpm: object) -> float | None:
    if bpm is None:
        return None
    try:
        bf = float(bpm)
        if bf > 0 and math.isfinite(bf):
            return bf
    except (TypeError, ValueError):
        pass
    return None


def _parse_rating(rating: object, notes: dict[str, int]) -> int | None:
    if rating is None:
        return None
    try:
        ri = int(rating)
        if 1 <= ri <= 5:
            return ri
        if ri != 0:
            notes["rating dropped: outside 0..5"] += 1
    except (TypeError, ValueError):
        notes["rating dropped: outside 0..5"] += 1
    return None


def _parse_duration_ms(duration: object) -> int | None:
    if duration is None:
        return None
    try:
        ds = float(duration)
        if ds > 0:
            return round(ds * 1000)
    except (TypeError, ValueError):
        pass
    return None


def _parse_rgb(color: object) -> int | None:
    if color is None:
        return None
    try:
        ci = int(color)
        if 0 <= ci <= 0xFFFFFF:
            return ci
    except (TypeError, ValueError):
        pass
    return None


def _track_extensions(comment: object, genre: object, year: object) -> dict[str, str]:
    extensions: dict[str, str] = {}
    if comment and str(comment).strip():
        extensions["x_comment"] = str(comment).strip()
    if genre and str(genre).strip():
        extensions["x_mixxx_genre"] = str(genre).strip()
    if year is not None and str(year).strip() not in ("", "0"):
        extensions["x_mixxx_year"] = str(year).strip()
    return extensions


def _attach_cues(
    conn: sqlite3.Connection,
    cols: dict[str, set[str]],
    tracks: list[Track],
    samplerates: dict[str, int],
    notes: dict[str, int],
) -> list[Track]:
    cues_by_track = _fetch_cues(conn, cols)
    updated: list[Track] = []
    for track in tracks:
        cue_points, cue_notes = _convert_cues(
            cues_by_track.get(track.track_id, ()),
            samplerates.get(track.track_id, 0),
        )
        for k, v in cue_notes.items():
            notes[k] += v
        updated.append(replace_track_cues(track, cue_points))
    return updated


def replace_track_cues(track: Track, cue_points: list[CuePoint]) -> Track:
    return Track(
        track_id=track.track_id,
        file_path=track.file_path,
        title=track.title,
        artists=track.artists,
        album=track.album,
        bpm=track.bpm,
        key_camelot=track.key_camelot,
        rating=track.rating,
        duration_ms=track.duration_ms,
        play_count=track.play_count,
        color_rgb=track.color_rgb,
        extensions=track.extensions,
        cues=tuple(cue_points),
        source_modified_at=track.source_modified_at,
    )


def _guard_schema(conn: sqlite3.Connection, db_path: Path) -> None:
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for name in _REQUIRED_TABLES:
        if name not in present:
            raise ValueError(f"{db_path}: not a Mixxx library (missing table {name})")


def _column_flags(conn: sqlite3.Connection) -> dict[str, set[str]]:
    flags: dict[str, set[str]] = {}
    for table in ("library", "cues"):
        flags[table] = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return flags


def _fetch_tracks(conn: sqlite3.Connection, cols: dict[str, set[str]]) -> list[tuple]:
    lib_color = "l.color" if "color" in cols["library"] else "NULL"
    lib_mixxx_del = "l.mixxx_deleted" if "mixxx_deleted" in cols["library"] else "0"
    lib_comment = "l.comment" if "comment" in cols["library"] else "NULL"
    sql = f"""
        SELECT l.id, l.title, l.artist, l.album, l.genre, l.year, l.bpm, l.key,
               l.rating, l.duration, l.timesplayed, {lib_color}, l.samplerate,
               {lib_comment}, tl.location, COALESCE(tl.fs_deleted, 0),
               COALESCE({lib_mixxx_del}, 0)
        FROM library l
        LEFT JOIN track_locations tl ON tl.id = l.location
        ORDER BY l.id
    """
    return conn.execute(sql).fetchall()


def _fetch_cues(conn: sqlite3.Connection, cols: dict[str, set[str]]) -> dict[str, list[tuple]]:
    cue_color = "color" if "color" in cols["cues"] else "NULL"
    sql = f"""
        SELECT track_id, type, position, length, hotcue, label, {cue_color}
        FROM cues ORDER BY track_id, id
    """
    by_track: dict[str, list[tuple]] = defaultdict(list)
    for row in conn.execute(sql).fetchall():
        by_track[str(row[0])].append(row)
    return by_track


def _samples_to_ms(position: float | int, samplerate: int) -> int:
    return round(float(position) * 1000 / (2 * samplerate))


def _convert_cues(rows: list[tuple], samplerate: int) -> tuple[list[CuePoint], dict[str, int]]:
    notes: dict[str, int] = defaultdict(int)
    if samplerate <= 0:
        if rows:
            notes["cues dropped: no samplerate in Mixxx"] += len(rows)
        return [], dict(notes)

    points: list[CuePoint] = []
    order = 0
    for row in rows:
        point, order_delta, note = _cue_row_to_point(row, samplerate, order)
        if note:
            notes[note] += 1
        elif point is not None:
            points.append(point)
            order += order_delta
    return points, dict(notes)


def _cue_row_to_point(row: tuple, samplerate: int, order: int) -> tuple[CuePoint | None, int, str | None]:
    _, ctype, position, length, hotcue, label, color = row
    if position is None or float(position) < 0:
        return None, 0, "cues dropped: no position"
    pos_ms = _samples_to_ms(position, samplerate)
    cue_color = _parse_rgb(color)
    name = str(label or "")
    hi = int(hotcue) if hotcue is not None else -1

    if ctype == 1:
        if hi < 0:
            return (
                CuePoint(index=order, position_ms=pos_ms, type="memory", name=name, color_rgb=cue_color),
                1,
                None,
            )
        return CuePoint(index=hi, position_ms=pos_ms, type="hot", name=name, color_rgb=cue_color), 1, None
    if ctype == 2:
        return CuePoint(index=0, position_ms=pos_ms, type="load", name=name, color_rgb=cue_color), 1, None
    if ctype == 4:
        if length is None or float(length) <= 0:
            return None, 0, "cues dropped: loop with no length"
        length_ms = _samples_to_ms(length, samplerate)
        idx = hi if hi >= 0 else order
        return (
            CuePoint(
                index=idx,
                position_ms=pos_ms,
                type="loop",
                name=name,
                length_ms=length_ms,
                color_rgb=cue_color,
            ),
            1,
            None,
        )
    return None, 0, f"cues dropped: Mixxx cue type {ctype} has no open-dj kind"


def _read_playlists(conn: sqlite3.Connection, kept: set[str], notes: dict[str, int]) -> list[Playlist]:
    playlists: list[Playlist] = []
    for pl_id, name, hidden in conn.execute("SELECT id, name, hidden FROM Playlists ORDER BY position, id").fetchall():
        if hidden and int(hidden) != 0:
            notes["playlist dropped: hidden in Mixxx (Auto DJ or set log)"] += 1
            continue
        members: list[str] = []
        for (track_id,) in conn.execute(
            "SELECT track_id FROM PlaylistTracks WHERE playlist_id=? ORDER BY position, id",
            (pl_id,),
        ).fetchall():
            tid = str(track_id)
            if tid in kept:
                members.append(tid)
            else:
                notes["playlist entry dropped: track not imported from Mixxx"] += 1
        playlists.append(Playlist(name=str(name or ""), track_ids=tuple(members)))
    return playlists


def _read_crates(conn: sqlite3.Connection, kept: set[str], notes: dict[str, int]) -> list[Playlist]:
    crates: list[Playlist] = []
    for crate_id, name in conn.execute("SELECT id, name FROM crates ORDER BY name, id").fetchall():
        members: list[str] = []
        for (track_id,) in conn.execute(
            "SELECT track_id FROM crate_tracks WHERE crate_id=? ORDER BY track_id",
            (crate_id,),
        ).fetchall():
            tid = str(track_id)
            if tid in kept:
                members.append(tid)
            else:
                notes["playlist entry dropped: track not imported from Mixxx"] += 1
        crates.append(Playlist(name=str(name or ""), track_ids=tuple(members)))
    return crates
