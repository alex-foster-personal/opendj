"""stable_id -> rekordbox asset resolution for the /performance parity UI.

Resolution chain (COMPONENT-MAP.md section 2, RECON-DATA.md section 1):

    state.db ``tracks`` -> ``track_vendor_ids`` (vendor='rekordbox')
    -> vendor_id -> ``djmdContent`` row in ``data/master.plain.db``
    (decrypted copy, opened read-only) -> FolderPath / ImagePath /
    AnalysisDataPath resolved on disk via the share-root rule.

ANLZ tags read (pyrekordbox 0.4.4; ``AnlzFile.tags`` is a LIST):

    * beatgrid:         .DAT PQTZ (get_beats / get_bpms / get_times)
    * tri-band preview: .2EX PWV6 (raw ``content.entries``, N x 3 bytes)
    * tri-band detail:  .2EX PWV7 (raw ``content.entries``, N x 3 bytes)
    * mono fallback:    .DAT PWAV (400 heights 0..31), .EXT PWV3 heights
    * phrases:          .EXT PSSI (beat-indexed; times via PQTZ)

PWV6/PWV7 ``tag.get()`` raises ``KeyError: 0`` in pyrekordbox 0.4.4
(RECON-DATA.md section 3), so the raw 3-byte entries are decoded here.
Byte order is (mid, high, low), scale 0..127 -- verified empirically on
this library: byte 2 is beat-locked and transient (kick -> low), byte 1
is offbeat-weighted (hats -> high), byte 0 sustained (mid).

Cues come from ``djmdCue`` in master.plain.db, NOT from ANLZ PCOB/PCO2
(empty in rekordbox 6/7 -- RECON-DATA.md section 4). Kind 9-11 rows are
excluded (slot mapping unverified -- see PARITY-TODO).

Fail-fast: every unresolved step raises an explicit HTTPException with a
``{"code", "message"}`` detail; there is no silent None anywhere.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
from fastapi import HTTPException

from apps.shared.paths import DATA_DIR, STATE_DB

log = logging.getLogger(__name__)

MASTER_PLAIN_DB: Path = DATA_DIR / "master.plain.db"
SHARE_ROOT: Path = Path.home() / "Library" / "Pioneer" / "rekordbox" / "share"
ANLZ_CACHE_DIR: Path = DATA_DIR / "state" / "anlz-cache"

AUDIO_MEDIA_TYPES: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
}
STREAMING_PREFIXES: tuple[str, ...] = ("tidal:", "soundcloud:", "spotify:")
ARTWORK_FILENAMES: dict[str, str] = {
    "s": "artwork_s.jpg",
    "m": "artwork_m.jpg",
    "orig": "artwork.jpg",
}
HOT_CUE_SLOTS: str = "ABCDEFGH"
# PWV6/PWV7 raw byte columns -> band names (see module docstring).
_BAND_COLUMNS: tuple[tuple[str, int], ...] = (("low", 2), ("mid", 0), ("high", 1))
_TRI_SCALE: float = 127.0
_MONO_SCALE: float = 31.0


@dataclass(frozen=True)
class RbContent:
    """One resolved djmdContent row (only the columns rb-assets needs)."""

    stable_id: str
    vendor_id: str
    folder_path: Optional[str]
    image_path: Optional[str]
    analysis_data_path: Optional[str]
    length_s: Optional[int]
    comment: Optional[str]
    genre: Optional[str]


# ----- errors + connections ------------------------------------------------

def not_found(code: str, message: str) -> HTTPException:
    """404 with the explicit {code, message} detail shape (COMPONENT-MAP 2)."""
    return HTTPException(status_code=404, detail={"code": code, "message": message})


def _open_ro(path: Path, label: str) -> sqlite3.Connection:
    if not path.exists():
        raise HTTPException(
            status_code=500,
            detail={
                "code": f"{label}_UNAVAILABLE",
                "message": f"required database missing on disk: {path}",
            },
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


# ----- stable_id -> djmdContent ---------------------------------------------

def resolve_content(stable_id: str) -> RbContent:
    """Resolve a stable_id to its rekordbox content row, failing explicitly."""
    state = _open_ro(STATE_DB, "STATE_DB")
    try:
        track_row = state.execute(
            "SELECT 1 FROM tracks WHERE stable_id = ?", (stable_id,)
        ).fetchone()
        if track_row is None:
            raise not_found("TRACK_NOT_FOUND", f"unknown stable_id {stable_id}")
        vendor_row = state.execute(
            "SELECT vendor_id FROM track_vendor_ids "
            "WHERE stable_id = ? AND vendor = 'rekordbox'",
            (stable_id,),
        ).fetchone()
        if vendor_row is None:
            raise not_found(
                "VENDOR_MAPPING_NOT_FOUND",
                f"no rekordbox vendor mapping for stable_id {stable_id}",
            )
        vendor_id = str(vendor_row[0])
    finally:
        state.close()

    master = _open_ro(MASTER_PLAIN_DB, "MASTER_DB")
    try:
        content = master.execute(
            "SELECT c.FolderPath, c.ImagePath, c.AnalysisDataPath, c.Length, "
            "       c.Commnt, g.Name "
            "FROM djmdContent c "
            "LEFT JOIN djmdGenre g ON g.ID = c.GenreID AND g.rb_local_deleted = 0 "
            "WHERE c.ID = ? AND c.rb_local_deleted = 0",
            (vendor_id,),
        ).fetchone()
    finally:
        master.close()
    if content is None:
        raise not_found(
            "VENDOR_MAPPING_NOT_FOUND",
            f"vendor_id {vendor_id} (stable_id {stable_id}) has no live "
            f"djmdContent row in {MASTER_PLAIN_DB.name}",
        )
    folder_path, image_path, analysis_data_path, length_s, comment, genre = content
    return RbContent(
        stable_id=stable_id,
        vendor_id=vendor_id,
        folder_path=folder_path or None,
        image_path=image_path or None,
        analysis_data_path=analysis_data_path or None,
        length_s=int(length_s) if length_s is not None else None,
        comment=comment or None,
        genre=genre or None,
    )


def resolve_share_path(path: str) -> Path:
    """RECON-DATA.md section 1: /PIONEER/ paths are share-relative."""
    if path.startswith("/PIONEER/"):
        return SHARE_ROOT / path.lstrip("/")
    return Path(path)


def is_streaming_path(folder_path: Optional[str]) -> bool:
    return bool(folder_path) and str(folder_path).startswith(STREAMING_PREFIXES)


# ----- per-asset resolution --------------------------------------------------

def audio_file(content: RbContent) -> tuple[Path, str]:
    """Resolve the on-disk audio file + media type, or 404 explicitly."""
    if not content.folder_path:
        raise not_found(
            "AUDIO_FILE_MISSING",
            f"track {content.stable_id} has no FolderPath in rekordbox",
        )
    if is_streaming_path(content.folder_path):
        raise not_found(
            "AUDIO_IS_STREAMING_URI",
            f"track {content.stable_id} is a streaming row "
            f"({content.folder_path.split(':', 1)[0]}:) with no local file",
        )
    path = resolve_share_path(content.folder_path)
    if not path.is_file():
        raise not_found(
            "AUDIO_FILE_MISSING",
            f"audio file for track {content.stable_id} does not exist on "
            f"disk: {path}",
        )
    media_type = AUDIO_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "AUDIO_FORMAT_UNSUPPORTED",
                "message": f"unsupported audio extension {path.suffix!r}: {path}",
            },
        )
    return path, media_type


def artwork_file(content: RbContent, size: str) -> Path:
    """Resolve the artwork jpg for a size variant (s/m/orig), or 404."""
    if size not in ARTWORK_FILENAMES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ARTWORK_SIZE_INVALID",
                "message": f"size must be one of {sorted(ARTWORK_FILENAMES)}",
            },
        )
    if not content.image_path:
        raise not_found(
            "ARTWORK_NOT_FOUND",
            f"track {content.stable_id} has no ImagePath in rekordbox",
        )
    # ImagePath points at .../artwork.jpg; siblings artwork_s / artwork_m.
    path = resolve_share_path(content.image_path).parent / ARTWORK_FILENAMES[size]
    if not path.is_file():
        raise not_found(
            "ARTWORK_NOT_FOUND",
            f"artwork file for track {content.stable_id} missing on disk: {path}",
        )
    return path


def anlz_dir(content: RbContent) -> Path:
    """Resolve the ANLZ directory (contains ANLZ0000.DAT/.EXT/.2EX), or 404."""
    if not content.analysis_data_path:
        raise not_found(
            "ANALYSIS_NOT_FOUND",
            f"track {content.stable_id} has no AnalysisDataPath in rekordbox",
        )
    dat = resolve_share_path(content.analysis_data_path)
    if not dat.is_file():
        raise not_found(
            "ANALYSIS_NOT_FOUND",
            f"ANLZ file for track {content.stable_id} missing on disk: {dat}",
        )
    return dat.parent


# ----- playlist ordering (djmdPlaylist Seq) -----------------------------------

def playlist_order_index() -> dict[str, int]:
    """djmdPlaylist ID -> flattened rekordbox tree position (0-based).

    Rekordbox orders the playlist tree by (ParentID, Seq) - a user-managed
    custom order, NOT alphabetical (SCREENSHOT-SPEC 5b). The flat /performance
    tree needs one comparable number per playlist, so the tree is walked
    depth-first from 'root' with siblings ordered by Seq; the visit order is
    the index. Folders are included (they carry Seq too and may map to
    playlists elsewhere); unknown parents simply never get visited and their
    subtrees stay absent from the map - a real data state the caller must
    treat as 'no rekordbox order known'.
    """
    master = _open_ro(MASTER_PLAIN_DB, "MASTER_DB")
    try:
        rows = master.execute(
            "SELECT ID, ParentID, Seq FROM djmdPlaylist WHERE rb_local_deleted = 0"
        ).fetchall()
    finally:
        master.close()

    children: dict[str, list[tuple[int, str]]] = {}
    for pl_id, parent_id, seq in rows:
        children.setdefault(str(parent_id), []).append(
            (int(seq) if seq is not None else 0, str(pl_id))
        )
    order: dict[str, int] = {}
    stack: list[str] = [
        pl_id for _, pl_id in sorted(children.get("root", []), reverse=True)
    ]
    while stack:
        pl_id = stack.pop()
        order[pl_id] = len(order)
        stack.extend(c for _, c in sorted(children.get(pl_id, []), reverse=True))
    return order


# ----- cues (djmdCue, NOT ANLZ) ----------------------------------------------

def fetch_cues(vendor_id: str) -> list[dict[str, Any]]:
    """Live djmdCue rows mapped to the COMPONENT-MAP 2.3 cue shape."""
    master = _open_ro(MASTER_PLAIN_DB, "MASTER_DB")
    try:
        rows = master.execute(
            "SELECT Kind, InMsec, OutMsec, ActiveLoop, BeatLoopSize, "
            "       ColorTableIndex, Comment "
            "FROM djmdCue WHERE ContentID = ? AND rb_local_deleted = 0",
            (vendor_id,),
        ).fetchall()
    finally:
        master.close()

    cues: list[dict[str, Any]] = []
    for kind_i, in_ms, out_ms, active_loop, loop_size, color_idx, comment in rows:
        if kind_i is None or not 0 <= int(kind_i) <= 8:
            continue  # Kind 9-11 excluded v1: slot mapping unverified (PARITY-TODO)
        is_loop = bool(out_ms and int(out_ms) > 0)
        if int(kind_i) == 0:
            kind = "loop" if is_loop else "memory"
            slot: Optional[str] = None
        else:
            kind = "hot_cue"
            slot = HOT_CUE_SLOTS[int(kind_i) - 1]
        cues.append({
            "kind": kind,
            "slot": slot,
            "in_ms": int(in_ms) if in_ms is not None else None,
            "out_ms": int(out_ms) if is_loop else None,
            "is_loop": is_loop,
            "active_loop": bool(active_loop),
            "beat_loop_size": int(loop_size) if loop_size is not None else None,
            "color_table_index": int(color_idx) if color_idx is not None else None,
            "comment": comment or None,
        })
    cues.sort(key=lambda c: c["in_ms"] if c["in_ms"] is not None else -1)
    return cues


def count_cues(vendor_id: str) -> int:
    """All live djmdCue rows for the track (incl. Kind 9-11, for rb-meta)."""
    master = _open_ro(MASTER_PLAIN_DB, "MASTER_DB")
    try:
        row = master.execute(
            "SELECT COUNT(*) FROM djmdCue "
            "WHERE ContentID = ? AND rb_local_deleted = 0",
            (vendor_id,),
        ).fetchone()
        return int(row[0])
    finally:
        master.close()


# ----- ANLZ payload (waveform + beatgrid + phrases) ---------------------------

def _downsample_max(arr: np.ndarray, points: int) -> np.ndarray:
    """Per-bucket max downsample along axis 0 to <= ``points`` entries."""
    n = arr.shape[0]
    if n <= points:
        return arr
    edges = (np.arange(points) * n) // points
    return np.maximum.reduceat(arr, edges, axis=0)


def _bands_payload(bands: dict[str, np.ndarray], points: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    length = 0
    for name, arr in bands.items():
        down = _downsample_max(arr, points)
        length = int(down.shape[0])
        out[name] = [round(float(v), 4) for v in down]
    out["length"] = length
    return out


def _first_tags(directory: Path) -> dict[str, Any]:
    """First occurrence of each tag type across the track's ANLZ files."""
    from pyrekordbox.anlz import read_anlz_files

    tags: dict[str, Any] = {}
    for anlz_file in read_anlz_files(directory).values():
        for tag in anlz_file.tags:  # tags is a LIST in pyrekordbox 0.4.4
            tags.setdefault(tag.type, tag)
    return tags


def _tri_bands(tag: Any) -> dict[str, np.ndarray]:
    """Decode raw PWV6/PWV7 3-byte entries -> low/mid/high floats 0..1."""
    raw = np.frombuffer(tag.content.entries, dtype=np.uint8).reshape(-1, 3)
    scaled = np.clip(raw.astype(np.float64) / _TRI_SCALE, 0.0, 1.0)
    return {name: scaled[:, col] for name, col in _BAND_COLUMNS}


def _mono_bands(tag: Any) -> dict[str, np.ndarray]:
    """PWAV/PWV3 heights (0..31) duplicated across all 3 band keys.

    kind="mono" declares to the client that these are single-band heights,
    not synthesised tri-band data; the values themselves are real.
    """
    heights = np.asarray(tag.get()[0], dtype=np.float64)
    scaled = np.clip(heights / _MONO_SCALE, 0.0, 1.0)
    return {name: scaled for name, _ in _BAND_COLUMNS}


def _beatgrid_payload(tags: dict[str, Any]) -> tuple[dict[str, Any], list[float]]:
    pqtz = tags.get("PQTZ")
    if pqtz is None:
        return {"beat_count": 0, "beats": []}, []
    beats = pqtz.get_beats()
    bpms = pqtz.get_bpms()
    times = [float(t) for t in pqtz.get_times()]
    grid = {
        "beat_count": len(beats),
        "beats": [
            {"n": int(n), "bpm": round(float(bpm), 2), "t": round(t, 3)}
            for n, bpm, t in zip(beats, bpms, times)
        ],
    }
    return grid, times


def _phrases_payload(tags: dict[str, Any], times: list[float]) -> list[dict[str, Any]]:
    pssi = tags.get("PSSI")
    if pssi is None or not times:
        return []

    def time_of_beat(beat: int) -> float:
        idx = min(max(beat - 1, 0), len(times) - 1)
        return round(times[idx], 3)

    mood = int(pssi.content.mood)
    end_beat = int(pssi.content.end_beat)
    entries = list(pssi.content.entries)
    phrases: list[dict[str, Any]] = []
    for i, entry in enumerate(entries):
        start_beat = int(entry.beat)
        stop_beat = int(entries[i + 1].beat) if i + 1 < len(entries) else end_beat
        phrases.append({
            "start_s": time_of_beat(start_beat),
            "end_s": time_of_beat(stop_beat),
            "kind": int(entry.kind),
            "mood": mood,
        })
    return phrases


def _anlz_mtime(directory: Path) -> float:
    files = sorted(directory.glob("ANLZ*"))
    if not files:
        raise not_found(
            "ANALYSIS_NOT_FOUND", f"no ANLZ files in directory {directory}"
        )
    return max(f.stat().st_mtime for f in files)


def _cache_path(stable_id: str) -> Path:
    return ANLZ_CACHE_DIR / f"{stable_id}.json"


def _load_cached_payload(
    stable_id: str, anlz_mtime: float, points: int
) -> Optional[dict[str, Any]]:
    path = _cache_path(stable_id)
    if not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("anlz cache unreadable, recomputing: %s (%s)", path, exc)
        return None
    if cached.get("anlz_mtime") == anlz_mtime and cached.get("points") == points:
        return cached["payload"]
    return None


def build_anlz_payload(content: RbContent, points: int) -> dict[str, Any]:
    """Parse ANLZ + djmdCue into the COMPONENT-MAP 2.3 JSON, with file cache.

    Cache: data/state/anlz-cache/{stable_id}.json keyed on (anlz file mtime,
    points); any mismatch recomputes and rewrites. Cues are always overlaid from
    the live ``djmdCue`` rows because they can change without touching ANLZ files.
    """
    directory = anlz_dir(content)
    anlz_mtime = _anlz_mtime(directory)
    cached = _load_cached_payload(content.stable_id, anlz_mtime, points)
    if cached is not None:
        payload = dict(cached)
        payload["cues"] = fetch_cues(content.vendor_id)
        return payload

    tags = _first_tags(directory)
    if "PWV6" in tags and "PWV7" in tags:
        kind = "tri"
        preview_bands = _tri_bands(tags["PWV6"])
        detail_bands = _tri_bands(tags["PWV7"])
    else:
        kind = "mono"
        preview_bands = _mono_bands(tags["PWAV"]) if "PWAV" in tags else {}
        detail_bands = _mono_bands(tags["PWV3"]) if "PWV3" in tags else {}
    empty_bands = {"length": 0, "low": [], "mid": [], "high": []}
    beatgrid, times = _beatgrid_payload(tags)
    payload: dict[str, Any] = {
        "stable_id": content.stable_id,
        "points": points,
        "waveform": {
            "kind": kind,
            "preview": _bands_payload(preview_bands, points)
            if preview_bands else dict(empty_bands),
            "detail": _bands_payload(detail_bands, points)
            if detail_bands else dict(empty_bands),
        },
        "beatgrid": beatgrid,
        "phrases": _phrases_payload(tags, times),
    }

    ANLZ_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(content.stable_id).write_text(
        json.dumps({
            "anlz_mtime": anlz_mtime,
            "points": points,
            "payload": payload,
        }),
        encoding="utf-8",
    )
    payload["cues"] = fetch_cues(content.vendor_id)
    return payload


__all__ = [
    "ANLZ_CACHE_DIR",
    "ARTWORK_FILENAMES",
    "AUDIO_MEDIA_TYPES",
    "HOT_CUE_SLOTS",
    "MASTER_PLAIN_DB",
    "RbContent",
    "SHARE_ROOT",
    "STREAMING_PREFIXES",
    "anlz_dir",
    "artwork_file",
    "audio_file",
    "build_anlz_payload",
    "count_cues",
    "fetch_cues",
    "is_streaming_path",
    "not_found",
    "resolve_content",
    "resolve_share_path",
]
