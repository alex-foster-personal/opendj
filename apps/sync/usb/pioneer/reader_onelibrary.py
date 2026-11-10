"""Read a rekordbox OneLibrary ``exportLibrary.db`` into an :class:`OpenDjLibrary`.

The SQLCipher handle is :mod:`apps.sync.usb.pioneer.onelibrary`. This module
maps tracks, playlists (in stick order), cue rows, beat grid anchors and
``odjAnalysisScalar`` loudness into the open-dj typed layer. Hot cues stored
only in USB ANLZ files are out of scope here (see issue #10050).

Requirement: CAT-06, DJIMPORT-10742.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.open_dj import AdapterReport, BeatGridPoint, CuePoint, OpenDjLibrary, Playlist, Track
from apps.open_dj.schema import CueType
from apps.shared.harmonic import key_to_camelot
from apps.sync.usb.pioneer.onelibrary import OneLibrary, OneLibraryError, SQLCIPHER_AVAILABLE
from apps.sync.usb.pioneer.value_verify_sidecar import read_scalar_sidecar_from_onelibrary

READER_AVAILABLE = SQLCIPHER_AVAILABLE

# Rekordbox OneLibrary ``cue.kind``: hot pads 1..8, 0 memory, 4 loop (pyrekordbox models).
_HOT_KIND_LO = 1
_HOT_KIND_HI = 8
_LOOP_KIND = 4
_MEMORY_KIND = 0


@dataclass
class OneLibraryRead:
    """Parsed export plus reader counters."""

    library: OpenDjLibrary
    report: AdapterReport = field(default_factory=AdapterReport)


def read_onelibrary(
    source: str | Path,
    *,
    stick_root: Path | None = None,
) -> OneLibraryRead:
    """Open ``exportLibrary.db`` at ``source`` and return an ``OpenDjLibrary``.

    When ``stick_root`` is set, ``content.analysis_data_file_path`` is resolved
    under that volume and a full PQTZ beat grid is read when the ANLZ files exist.
    Otherwise each track's grid is a single anchor at 0 ms from ``bpmx100``.
    """
    db_path = Path(source)
    if not READER_AVAILABLE:
        raise OneLibraryError("sqlcipher3 is unavailable; cannot read OneLibrary")
    report = AdapterReport()
    root = Path(stick_root).resolve() if stick_root is not None else None

    sidecar_by_content_id, _contents_by_filename, unread = read_scalar_sidecar_from_onelibrary(db_path)
    for note in unread:
        report.bump("loudness_unread")
        report.warn(
            field="loudness",
            track_id="*",
            action="dropped",
            reason=note,
        )

    with OneLibrary(db_path, readonly=True) as db:
        artists = {int(r["id"]): str(r.get("name") or "") for r in db.get_artists()}
        keys = {int(r["id"]): str(r.get("name") or "") for r in db.get_keys()}
        albums = {int(r["id"]): str(r.get("name") or "") for r in db.get_albums()}
        genres = {int(r["id"]): str(r.get("name") or "") for r in db.get_genres()}
        cues_by_content = _load_cues(db)
        tracks = [
            _content_to_track(
                row,
                artists=artists,
                keys=keys,
                albums=albums,
                genres=genres,
                cues=cues_by_content.get(int(row["id"]), ()),
                loudness=sidecar_by_content_id.get(int(row["id"]), {}),
                stick_root=root,
                report=report,
            )
            for row in db.get_contents()
        ]
        playlists = _read_playlists(db, track_id_for_content=_track_id)

    library = OpenDjLibrary(version="0.1", tracks=tuple(tracks), playlists=tuple(playlists))
    return OneLibraryRead(library=library, report=report)


def _track_id(content_id: int) -> str:
    return f"onelibrary:{int(content_id)}"


def _load_cues(db: OneLibrary) -> dict[int, tuple[CuePoint, ...]]:
    if not db.has_table("cue"):
        return {}
    rows = db.iter_table("cue")
    out: dict[int, list[CuePoint]] = {}
    for row in rows:
        if row.get("content_id") is None:
            continue
        content_id = int(row["content_id"])
        cue = _cue_row_to_point(row)
        if cue is None:
            continue
        out.setdefault(content_id, []).append(cue)
    return {cid: tuple(sorted(points, key=lambda p: (p.position_ms, p.index, p.type))) for cid, points in out.items()}


def _cue_row_to_point(row: Mapping[str, Any]) -> CuePoint | None:
    kind_raw = row.get("kind")
    if kind_raw is None:
        return None
    kind = int(kind_raw)
    in_usec = row.get("in_usec") if "in_usec" in row else row.get("inUsec")
    if in_usec is None:
        return None
    position_ms = int(int(in_usec) // 1000)
    name = str(row.get("cue_comment") or row.get("cueComment") or "")
    out_usec = row.get("out_usec") if "out_usec" in row else row.get("outUsec")
    length_ms: int | None = None
    if out_usec is not None:
        out_val = int(out_usec)
        if out_val > int(in_usec):
            length_ms = (out_val - int(in_usec)) // 1000

    cue_type: CueType
    index: int
    if _HOT_KIND_LO <= kind <= _HOT_KIND_HI:
        cue_type = "hot"
        index = kind - _HOT_KIND_LO
    elif kind == _LOOP_KIND or int(row.get("is_active_loop") or row.get("isActiveLoop") or 0):
        cue_type = "loop"
        index = 0
    elif kind == _MEMORY_KIND:
        cue_type = "memory"
        index = 0
    else:
        cue_type = "memory"
        index = 0

    if cue_type == "loop" and length_ms is None:
        length_ms = 0
    return CuePoint(
        index=index,
        position_ms=position_ms,
        type=cue_type,
        name=name,
        length_ms=length_ms if cue_type == "loop" else None,
    )


def _content_to_track(
    row: Mapping[str, Any],
    *,
    artists: Mapping[int, str],
    keys: Mapping[int, str],
    albums: Mapping[int, str],
    genres: Mapping[int, str],
    cues: Sequence[CuePoint],
    loudness: Mapping[str, str],
    stick_root: Path | None,
    report: AdapterReport,
) -> Track:
    content_id = int(row["id"])
    artist_name = artists.get(int(row.get("artist_id") or 0), "")
    key_name = keys.get(int(row.get("key_id") or 0), "")
    key_camelot = key_to_camelot(key_name) if key_name else None
    bpm_raw = row.get("bpmx100")
    bpm = None if bpm_raw is None else round(int(bpm_raw) / 100.0, 2)
    path = str(row.get("path") or "")
    file_name = str(row.get("file_name") or row.get("fileName") or "")
    if stick_root is not None and path.startswith("/"):
        file_path = str((stick_root / path.lstrip("/")).resolve())
    elif path:
        file_path = path
    elif file_name:
        file_path = file_name
    else:
        file_path = f"onelibrary:{content_id}"
        report.bump("location_missing")

    beats = _beats_for_content(row, bpm=bpm, stick_root=stick_root, report=report, track_id=_track_id(content_id))
    extensions: dict[str, Any] = {}
    if genre_id := row.get("genre_id"):
        gname = genres.get(int(genre_id), "")
        if gname:
            extensions["x_genre"] = gname
    if album_id := row.get("album_id"):
        aname = albums.get(int(album_id), "")
        if aname:
            extensions["x_album"] = aname
    if lufs := loudness.get("loudness_lufs"):
        extensions["x_loudness_lufs"] = float(lufs)
    if dbtp := loudness.get("loudness_dbtp"):
        extensions["x_loudness_dbtp"] = float(dbtp)

    rating_raw = row.get("rating")
    rating = None if rating_raw is None else int(rating_raw)

    return Track(
        track_id=_track_id(content_id),
        file_path=file_path,
        title=str(row.get("title") or ""),
        artists=(artist_name,) if artist_name else (),
        album=albums.get(int(row.get("album_id") or 0), ""),
        bpm=bpm,
        key_camelot=key_camelot,
        rating=rating,
        duration_ms=None if row.get("length") is None else int(row["length"]),
        play_count=int(row.get("dj_play_count") or 0),
        cues=tuple(cues),
        beats=beats,
        extensions=extensions,
    )


def _beats_for_content(
    row: Mapping[str, Any],
    *,
    bpm: float | None,
    stick_root: Path | None,
    report: AdapterReport,
    track_id: str,
) -> tuple[BeatGridPoint, ...]:
    analyze_path = row.get("analysis_data_file_path") or row.get("analysisDataFilePath")
    if stick_root is not None and analyze_path:
        try:
            from apps.sync.usb.pioneer.anlz_track import read_stick_track_analysis

            parsed = read_stick_track_analysis(
                volume_root=stick_root,
                analyze_path=str(analyze_path),
                points=400,
            )
        except Exception as exc:
            report.bump("grid_unread")
            report.warn(field="beats", track_id=track_id, action="dropped", reason=str(exc))
        else:
            grid = parsed.payload.get("beatgrid", {})
            raw_beats = grid.get("beats") if isinstance(grid, dict) else None
            if raw_beats:
                points: list[BeatGridPoint] = []
                for entry in raw_beats:
                    t_ms = int(round(float(entry["t"]) * 1000.0))
                    entry_bpm = float(entry.get("bpm", bpm or 0.0))
                    points.append(BeatGridPoint(position_ms=t_ms, bpm=entry_bpm, terminal=False))
                if points:
                    points[-1] = BeatGridPoint(
                        position_ms=points[-1].position_ms,
                        bpm=points[-1].bpm,
                        terminal=True,
                    )
                    return tuple(points)
    if bpm is None or not math.isfinite(bpm) or bpm <= 0:
        return ()
    return (BeatGridPoint(position_ms=0, bpm=float(bpm), terminal=True),)


def _read_playlists(db: OneLibrary, *, track_id_for_content) -> list[Playlist]:
    rows = db.get_playlists()
    by_parent: dict[int | None, list[dict[str, Any]]] = {}
    for row in rows:
        parent = int(row.get("parent_id") or 0)
        parent_key: int | None = None if parent == 0 else parent
        by_parent.setdefault(parent_key, []).append(row)

    def walk(parent_id: int | None) -> tuple[Playlist, ...]:
        siblings = sorted(by_parent.get(parent_id, ()), key=lambda r: int(r.get("seq") or 0))
        out: list[Playlist] = []
        for row in siblings:
            is_folder = int(row.get("attribute") or 0) == 1
            name = str(row.get("name") or "")
            if is_folder:
                children = walk(int(row["id"]))
                out.append(Playlist(name=name, track_ids=(), children=children))
            else:
                track_ids = tuple(track_id_for_content(int(c["id"])) for c in db.get_playlist_contents(int(row["id"])))
                out.append(Playlist(name=name, track_ids=track_ids, children=()))
        return tuple(out)

    return list(walk(None))
