"""Read VirtualDJ ``database.xml`` and ``MyLists`` into an ``OpenDjLibrary``.

Layout (VirtualDJ 8 / 2023, as documented in VirtualDJ's public manual)::

    <VirtualDJ_Database Version="8.2">
     <Song FilePath="/Volumes/Invented DJ/Music/a.mp3" FileSize="9812345">
      <Tags Author=".." Title=".." Album=".." Genre=".." Year="2019" Bpm="" Key="Am" Stars="4"
            Remix=".." Label=".." Composer=".." Grouping=".."/>
      <Infos SongLength="242.500" PlayCount="7" Bitrate="320" FirstSeen="1700000000" LastPlay="1730000000"/>
      <Comment>..</Comment>
      <Scan Version="801" Bpm="0.468750" Key="Am" Volume="1.05"/>
      <Poi Pos="0.025" Type="beatgrid"/>
      <Poi Pos="32.000" Type="cue" Num="1" Name="Drop"/>
      <Poi Pos="64.000" Type="loop" Size="7.500" Name="Loop A"/>
      <Poi Pos="1.000" Type="automix" Point="realStart"/>
     </Song>
    </VirtualDJ_Database>

Loop ``Poi`` ``Size`` is read as seconds (assumption; VirtualDJ's docs are not
reproduced here).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import math
import os
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.adapters.m3u import parse_m3u
from apps.open_dj import BeatGridPoint, CuePoint, OpenDjLibrary, Playlist, Track
from apps.shared.harmonic import key_to_camelot

_HOT_PADS = 8


def _nfc(path: str) -> str:
    return unicodedata.normalize("NFC", path)


@dataclass(frozen=True)
class VirtualDjRead:
    """A parsed VirtualDJ library plus reader-side counters."""

    library: OpenDjLibrary
    database: Path
    songs_seen: int = 0
    songs_streaming: int = 0
    bpm_as_period: int = 0
    bpm_as_bpm: int = 0
    bpm_dropped: int = 0
    keys_unrecognized: int = 0
    pois_skipped: dict[str, int] = field(default_factory=dict)
    windows_paths_unresolved: int = 0
    playlist_entries_unresolved: int = 0
    filter_folders_skipped: int = 0


def virtualdj_path_resolver(*, windows: bool = os.name == "nt") -> Callable[[str], str]:
    """On Windows normalize through ``Path``; otherwise keep the stored string."""

    def resolve(stored: str) -> str:
        if windows:
            return str(Path(stored))
        return stored

    return resolve


def read_virtualdj_library(source: Path, *, windows: bool = os.name == "nt") -> VirtualDjRead:
    source = Path(source).expanduser()
    database, mylists_dir = _resolve_paths(source)
    root = ET.parse(database).getroot()
    if root.tag != "VirtualDJ_Database":
        raise ValueError(f"{database} is not a VirtualDJ database (root <{root.tag}>)")

    stats = _ReadStats()
    tracks, path_to_id = _read_songs(database, root, stats, windows=windows)
    playlists = _read_mylists(mylists_dir, path_to_id, stats) if mylists_dir.is_dir() else ()
    library = OpenDjLibrary(version="0.1", tracks=tuple(tracks), playlists=playlists)
    return VirtualDjRead(
        library=library,
        database=database,
        songs_seen=stats.songs_seen,
        songs_streaming=stats.songs_streaming,
        bpm_as_period=stats.bpm_as_period,
        bpm_as_bpm=stats.bpm_as_bpm,
        bpm_dropped=stats.bpm_dropped,
        keys_unrecognized=stats.keys_unrecognized,
        pois_skipped=dict(stats.pois_skipped),
        windows_paths_unresolved=stats.windows_paths_unresolved,
        playlist_entries_unresolved=stats.playlist_entries_unresolved,
        filter_folders_skipped=stats.filter_folders_skipped,
    )


@dataclass
class _ReadStats:
    songs_seen: int = 0
    songs_streaming: int = 0
    bpm_as_period: int = 0
    bpm_as_bpm: int = 0
    bpm_dropped: int = 0
    keys_unrecognized: int = 0
    pois_skipped: dict[str, int] = field(default_factory=dict)
    windows_paths_unresolved: int = 0
    playlist_entries_unresolved: int = 0
    filter_folders_skipped: int = 0


def _resolve_paths(source: Path) -> tuple[Path, Path]:
    if source.is_file():
        database = source
        base = source.parent
    else:
        database = source / "database.xml"
        base = source
    if not database.is_file():
        raise FileNotFoundError(f"VirtualDJ database not found: {database}")
    return database, base / "MyLists"


def _track_id(path: str) -> str:
    digest = hashlib.sha256(_nfc(path).encode()).hexdigest()[:16]
    return f"vdj_{digest}"


def _is_windows_path(text: str) -> bool:
    if text.startswith("\\\\"):
        return True
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


def _float(raw: str | None, *, file_path: str, attr: str) -> float:
    text = (raw or "").strip()
    if not text:
        raise ValueError(f"{file_path}: missing {attr}")
    try:
        value = float(text.replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{file_path}: {attr}={raw!r} is not a number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{file_path}: {attr}={raw!r} is not finite")
    return value


def _optional_float(raw: str | None, *, file_path: str, attr: str) -> float | None:
    text = (raw or "").strip()
    if not text:
        return None
    return _float(text, file_path=file_path, attr=attr)


def _int_nonneg(raw: str | None, *, file_path: str, attr: str) -> int:
    text = (raw or "").strip()
    if not text:
        return 0
    try:
        value = int(text, 10)
    except ValueError as exc:
        raise ValueError(f"{file_path}: {attr}={raw!r} is not an integer") from exc
    if value < 0:
        raise ValueError(f"{file_path}: {attr}={raw!r} must be >= 0")
    return value


def _optional_pos_int(raw: str | None, *, file_path: str, attr: str) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = int(text, 10)
    except ValueError as exc:
        raise ValueError(f"{file_path}: {attr}={raw!r} is not an integer") from exc
    if value <= 0:
        raise ValueError(f"{file_path}: {attr}={raw!r} must be > 0")
    return value


def _optional_rating(raw: str | None, *, file_path: str) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        stars = int(text, 10)
    except ValueError as exc:
        raise ValueError(f"{file_path}: Tags Stars={raw!r} is not an integer") from exc
    if stars < 0 or stars > 5:
        raise ValueError(f"{file_path}: Tags Stars={raw!r} must be 0..5")
    return stars


def _bpm_value(
    tags_bpm: str | None,
    scan_bpm: str | None,
    stats: _ReadStats,
    file_path: str,
) -> tuple[float | None, str]:
    """Return (bpm, kind) where kind is period, bpm, dropped, or missing."""
    raw = (tags_bpm or "").strip() or (scan_bpm or "").strip()
    if not raw:
        return None, "missing"
    value = _float(raw, file_path=file_path, attr="Bpm")
    if value <= 0:
        return None, "missing"
    if value < 10:
        stats.bpm_as_period += 1
        bpm = round(60.0 / value, 3)
        kind = "period"
    else:
        stats.bpm_as_bpm += 1
        bpm = round(value, 3)
        kind = "bpm"
    if bpm < 10 or bpm > 999:
        if kind == "period":
            stats.bpm_as_period -= 1
        else:
            stats.bpm_as_bpm -= 1
        stats.bpm_dropped += 1
        return None, "dropped"
    return bpm, kind


def _read_key(tags_key: str | None, scan_key: str | None, stats: _ReadStats) -> str | None:
    raw = (tags_key or "").strip() or (scan_key or "").strip()
    if not raw:
        return None
    try:
        return str(key_to_camelot(raw))
    except ValueError:
        stats.keys_unrecognized += 1
        return None


def _unix_date_yyyy_mm_dd(raw: str | None, *, file_path: str, attr: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        seconds = int(text, 10)
    except ValueError as exc:
        raise ValueError(f"{file_path}: {attr}={raw!r} is not an integer timestamp") from exc
    return _dt.datetime.fromtimestamp(seconds, _dt.UTC).strftime("%Y-%m-%d")


def _unix_iso8601(raw: str | None, *, file_path: str, attr: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        seconds = int(text, 10)
    except ValueError as exc:
        raise ValueError(f"{file_path}: {attr}={raw!r} is not an integer timestamp") from exc
    return _dt.datetime.fromtimestamp(seconds, _dt.UTC).isoformat()


def _read_extensions(song: ET.Element, tags: ET.Element, infos: ET.Element, comment_el: ET.Element | None) -> dict[str, Any]:
    file_path = song.get("FilePath") or ""
    ext: dict[str, Any] = {}
    for tag_attr, key in (
        ("Genre", "x_genre"),
        ("Label", "x_label"),
        ("Remix", "x_remixer"),
        ("Composer", "x_composer"),
        ("Grouping", "x_grouping"),
    ):
        value = (tags.get(tag_attr) or "").strip()
        if value:
            ext[key] = value
    year = _optional_pos_int(tags.get("Year"), file_path=file_path, attr="Tags Year")
    if year is not None:
        ext["x_year"] = year
    bitrate = _optional_pos_int(infos.get("Bitrate"), file_path=file_path, attr="Infos Bitrate")
    if bitrate is not None:
        ext["x_bitrate"] = bitrate
    size = _optional_pos_int(song.get("FileSize"), file_path=file_path, attr="FileSize")
    if size is not None:
        ext["x_size"] = size
    if comment_el is not None and (comment_el.text or "").strip():
        ext["x_comment"] = (comment_el.text or "").strip()
    date_added = _unix_date_yyyy_mm_dd(infos.get("FirstSeen"), file_path=file_path, attr="Infos FirstSeen")
    if date_added:
        ext["x_date_added"] = date_added
    last_play = _unix_iso8601(infos.get("LastPlay"), file_path=file_path, attr="Infos LastPlay")
    if last_play:
        ext["x_virtualdj_last_play"] = last_play
    scan = song.find("Scan")
    if scan is not None:
        gain = _optional_float(scan.get("Volume"), file_path=file_path, attr="Scan Volume")
        if gain is not None:
            ext["x_virtualdj_gain"] = gain
    return ext


def _read_songs(
    database: Path,
    root: ET.Element,
    stats: _ReadStats,
    *,
    windows: bool,
) -> tuple[list[Track], dict[str, str]]:
    tracks: list[Track] = []
    path_to_id: dict[str, str] = {}
    seen_paths: dict[str, str] = {}
    for song in root.findall("Song"):
        stats.songs_seen += 1
        file_path = song.get("FilePath") or ""
        if not file_path.strip() or "://" in file_path:
            stats.songs_streaming += 1
            continue
        nfc_path = _nfc(file_path)
        if nfc_path in seen_paths:
            raise ValueError(
                f"{database}: duplicate FilePath {file_path!r} "
                f"(already on {seen_paths[nfc_path]!r})"
            )
        seen_paths[nfc_path] = file_path
        if not windows and _is_windows_path(file_path):
            stats.windows_paths_unresolved += 1
        tags = song.find("Tags")
        infos = song.find("Infos")
        if tags is None:
            tags = ET.Element("Tags")
        if infos is None:
            infos = ET.Element("Infos")
        comment_el = song.find("Comment")
        scan = song.find("Scan")
        scan_bpm = scan.get("Bpm") if scan is not None else None
        scan_key = scan.get("Key") if scan is not None else None
        try:
            song_length = _optional_float(infos.get("SongLength"), file_path=file_path, attr="Infos SongLength")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if song_length is not None and song_length < 0:
            raise ValueError(f"{file_path}: Infos SongLength must be >= 0")
        duration_ms = round(song_length * 1000) if song_length is not None else None
        play_count = _int_nonneg(infos.get("PlayCount"), file_path=file_path, attr="Infos PlayCount")
        rating = _optional_rating(tags.get("Stars"), file_path=file_path)
        bpm, _ = _bpm_value(tags.get("Bpm"), scan_bpm, stats, file_path)
        key_camelot = _read_key(tags.get("Key"), scan_key, stats)
        author = (tags.get("Author") or "").strip()
        artists = (author,) if author else ()
        try:
            cues, beats = _read_pois(song, file_path, bpm, stats)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        track_id = _track_id(file_path)
        path_to_id[nfc_path] = track_id
        tracks.append(
            Track(
                track_id=track_id,
                file_path=file_path,
                title=(tags.get("Title") or "").strip(),
                artists=artists,
                album=(tags.get("Album") or "").strip(),
                bpm=bpm,
                key_camelot=key_camelot,
                rating=rating,
                duration_ms=duration_ms,
                play_count=play_count,
                cues=tuple(cues),
                beats=beats,
                extensions=_read_extensions(song, tags, infos, comment_el),
            )
        )
    return tracks, path_to_id


def _skip_poi(stats: _ReadStats, kind: str) -> None:
    stats.pois_skipped[kind] = stats.pois_skipped.get(kind, 0) + 1


def _poi_position_ms(poi: ET.Element, file_path: str) -> int:
    raw = poi.get("Pos")
    if raw is None or not str(raw).strip():
        return 0
    pos_s = _float(raw, file_path=file_path, attr="Poi Pos")
    if pos_s < 0:
        raise ValueError(f"{file_path}: Poi Pos={poi.get('Pos')!r} must be >= 0")
    return round(pos_s * 1000)


def _poi_num(raw_num: str | None, file_path: str) -> int | None:
    if raw_num is None or not str(raw_num).strip():
        return None
    try:
        num = int(str(raw_num).strip(), 10)
    except ValueError as exc:
        raise ValueError(f"{file_path}: Poi Num={raw_num!r} is not an integer") from exc
    if num == -1:
        return None
    if num <= 0:
        raise ValueError(f"{file_path}: Poi Num={raw_num!r} must be >= 1")
    return num


def _poi_loop_length_ms(poi: ET.Element, file_path: str) -> int:
    size_raw = poi.get("Size")
    if size_raw is None or not str(size_raw).strip():
        raise ValueError(f"{file_path}: loop Poi missing Size")
    try:
        size_s = float(str(size_raw).strip().replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{file_path}: Poi Size={size_raw!r} is not a number") from exc
    if size_s <= 0:
        raise ValueError(f"{file_path}: Poi Size={size_raw!r} must be > 0")
    return round(size_s * 1000)


def _append_cue_poi(
    poi: ET.Element,
    file_path: str,
    position_ms: int,
    name: str,
    hot_used: set[int],
    poi_order: int,
) -> tuple[CuePoint, int]:
    num = _poi_num(poi.get("Num"), file_path)
    if num is not None and 1 <= num <= _HOT_PADS:
        index = num - 1
        if index in hot_used:
            raise ValueError(f"{file_path}: two cue Pois on pad Num={num}")
        hot_used.add(index)
        cue = CuePoint(index=index, position_ms=position_ms, type="hot", name=name)
    else:
        cue = CuePoint(index=poi_order, position_ms=position_ms, type="memory", name=name)
    return cue, poi_order + 1


def _append_loop_poi(
    poi: ET.Element,
    file_path: str,
    position_ms: int,
    name: str,
    poi_order: int,
) -> tuple[CuePoint, int]:
    length_ms = _poi_loop_length_ms(poi, file_path)
    index = poi_order
    num = _poi_num(poi.get("Num"), file_path)
    if num is not None and 1 <= num <= _HOT_PADS:
        index = num - 1
    cue = CuePoint(index=index, position_ms=position_ms, type="loop", name=name, length_ms=length_ms)
    return cue, poi_order + 1


def _append_beatgrid_poi(
    position_ms: int,
    track_bpm: float | None,
    stats: _ReadStats,
    beats: list[BeatGridPoint],
    beatgrid_seen: bool,
) -> bool:
    if beatgrid_seen:
        _skip_poi(stats, "beatgrid (extra anchor)")
        return True
    if track_bpm is None:
        _skip_poi(stats, "beatgrid (no bpm)")
    else:
        beats.append(BeatGridPoint(position_ms=position_ms, bpm=track_bpm))
    return True


def _read_pois(
    song: ET.Element,
    file_path: str,
    track_bpm: float | None,
    stats: _ReadStats,
) -> tuple[list[CuePoint], tuple[BeatGridPoint, ...]]:
    cues: list[CuePoint] = []
    beats: list[BeatGridPoint] = []
    hot_used: set[int] = set()
    poi_order = 0
    beatgrid_seen = False
    for poi in song.findall("Poi"):
        poi_type = (poi.get("Type") or "").strip().lower()
        position_ms = _poi_position_ms(poi, file_path)
        name = (poi.get("Name") or "").strip()

        if poi_type == "cue":
            cue, poi_order = _append_cue_poi(poi, file_path, position_ms, name, hot_used, poi_order)
            cues.append(cue)
            continue
        if poi_type == "loop":
            cue, poi_order = _append_loop_poi(poi, file_path, position_ms, name, poi_order)
            cues.append(cue)
            continue
        if poi_type == "beatgrid":
            beatgrid_seen = _append_beatgrid_poi(position_ms, track_bpm, stats, beats, beatgrid_seen)
            continue
        _skip_poi(stats, poi_type or "(none)")

    return cues, tuple(beats)


def _playlist_track_id(path: str, path_to_id: dict[str, str], stats: _ReadStats) -> str:
    key = _nfc(path)
    hit = path_to_id.get(key)
    if hit is not None:
        return hit
    stats.playlist_entries_unresolved += 1
    return _track_id(path)


def _read_vdjfolder(path: Path, path_to_id: dict[str, str], stats: _ReadStats) -> Playlist | None:
    root = ET.parse(path).getroot()
    if root.tag == "FilterFolder":
        stats.filter_folders_skipped += 1
        return None
    if root.tag != "VirtualFolder":
        raise ValueError(f"{path}: expected VirtualFolder or FilterFolder root, got <{root.tag}>")
    members: list[str] = []
    for song in root.findall("song"):
        entry = song.get("path")
        if entry is None or not str(entry).strip():
            raise ValueError(f"{path}: song element missing path attribute")
        members.append(_playlist_track_id(str(entry).strip(), path_to_id, stats))
    return Playlist(name=path.stem, track_ids=tuple(members))


def _read_m3u_playlist(path: Path, path_to_id: dict[str, str], stats: _ReadStats) -> Playlist:
    text = path.read_text(encoding="utf-8-sig")
    members: list[str] = []
    base = path.resolve().parent
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _is_windows_path(line):
            resolved = line
        else:
            parsed = parse_m3u(line + "\n", base)
            resolved = parsed[0] if parsed else line
        members.append(_playlist_track_id(resolved, path_to_id, stats))
    return Playlist(name=path.stem, track_ids=tuple(members))


def _read_mylists_dir(
    directory: Path,
    path_to_id: dict[str, str],
    stats: _ReadStats,
) -> tuple[Playlist, ...]:
    playlists: list[Playlist] = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir():
            children = _read_mylists_dir(entry, path_to_id, stats)
            if children:
                playlists.append(Playlist(name=entry.name, track_ids=(), children=tuple(children)))
            continue
        if entry.suffix.lower() == ".vdjfolder":
            pl = _read_vdjfolder(entry, path_to_id, stats)
            if pl is not None:
                playlists.append(pl)
            continue
        if entry.suffix.lower() in (".m3u", ".m3u8"):
            playlists.append(_read_m3u_playlist(entry, path_to_id, stats))
    return tuple(playlists)


def _read_mylists(
    mylists_dir: Path,
    path_to_id: dict[str, str],
    stats: _ReadStats,
) -> tuple[Playlist, ...]:
    if not mylists_dir.is_dir():
        return ()
    return _read_mylists_dir(mylists_dir, path_to_id, stats)
