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
    * vocal intensity:  .2EX PVDI (raw section walk; undocumented tag,
      decoded per SPIKE-B1 -- pyrekordbox drops it with a warning)
    * 120-col preview strips: .2EX PWV6 -> .EXT PWV4 -> .DAT PWAV via the
      raw PMAI section walker (SPIKE-A1; 12x faster than pyrekordbox and
      immune to its broken PWV6 ``tag.get()``)

PWV6/PWV7 ``tag.get()`` raises ``KeyError: 0`` in pyrekordbox 0.4.4
(RECON-DATA.md section 3), so the raw 3-byte entries are decoded here.
Byte order is (low, mid, high), scale 0..127 -- proven empirically in
SPIKE-A1 section 3 by Pearson-correlating each byte column against
ffmpeg-decoded band envelopes on the same 1200-column grid (byte 0
correlates 0.82-0.86 with the 20-150 Hz band, byte 1 with 500-2000 Hz,
byte 2 with 5-16 kHz). This supersedes the earlier (mid, high, low)
guess; the versioned anlz cache (ANLZ_CACHE_SCHEMA) self-heals any
payloads cached under the old ordering.

Cues come from ``djmdCue`` in master.plain.db, NOT from ANLZ PCOB/PCO2
(empty in rekordbox 6/7 -- RECON-DATA.md section 4). Kind 9-11 rows are
excluded (slot mapping unverified -- see PARITY-TODO).

Fail-fast: every unresolved step raises an explicit HTTPException with a
``{"code", "message"}`` detail; there is no silent None anywhere.
"""
from __future__ import annotations

import base64
import json
import logging
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

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
# PWV6/PWV7 raw byte columns -> band names (SPIKE-A1 section 3 proof).
_BAND_COLUMNS: tuple[tuple[str, int], ...] = (("low", 0), ("mid", 1), ("high", 2))
_TRI_SCALE: float = 127.0
_MONO_SCALE: float = 31.0

# Cached anlz JSON schema version. Bump whenever the payload shape or any
# decode semantics change (band order fix, vocals field, ...) so stale
# cache entries self-heal by recomputing instead of serving old shapes.
ANLZ_CACHE_SCHEMA: int = 2

# --- preview strip (SPIKE-A1 / SPIKE-SUMMARY section 2) ---
PREVIEW_COLUMNS: int = 120          # 1200 -> 120 peak-max downsample (10:1)
_PWV4_LUMINANCE_BYTE: int = 0       # verified: corr 0.60 vs PWV6 height, 0..127
_PWAV_HEIGHT_MASK: int = 0x1F       # low 5 bits = height 0..31 (A1 tag table)

# --- vocals (SPIKE-B1 / SPIKE-B2 calibrated params) ---
VOCAL_INTENSITY_MIN: int = 1        # PVDI frame value (0..4) counted as vocal
VOCAL_MERGE_GAP_S: float = 1.5      # merge regions separated by < this gap
VOCAL_MIN_REGION_S: float = 1.0     # drop merged regions shorter than this
# PVDI fixed header bytes at section offset 12..20: u16 reserved=0x0000,
# u16 hop=1024, u16 rate=22050, u16 version=1. Uniform across all 3985
# carriers (B1 section 3); any deviation means the format changed.
_PVDI_FIXED_HEADER: bytes = bytes.fromhex("0000040056220001")
_PVDI_HOP: int = 1024
_PVDI_RATE: int = 22050

# --- file-existence + preview caches ---
# file_exists is disk truth (FR-1 item 4): per-path stat results are cached
# for FILE_EXISTS_TTL_S so a listing request never stats 8k files -- one
# bulk stat pass warms the cache, then repeats are dict lookups until the
# TTL lapses (30 s keeps "file restored by reconcile" visible quickly).
FILE_EXISTS_TTL_S: float = 30.0
_FILE_EXISTS_LOCK = threading.Lock()
_FILE_EXISTS_CACHE: dict[str, tuple[float, bool]] = {}

# Preview strips are immutable per (source file, mtime): cache the encoded
# strip per AnalysisDataPath and revalidate with a single stat per hit.
# ~10k entries x ~500 B is a few MB; no eviction needed.
_PREVIEW_LOCK = threading.Lock()
_PREVIEW_CACHE: dict[str, tuple[str, float, str, int]] = {}

_SQL_CHUNK: int = 500               # keep IN (...) under SQLite's var cap


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


@dataclass(frozen=True)
class RbRowMeta:
    """Rekordbox columns a hydrated track row needs (bulk-resolved)."""

    vendor_id: str
    folder_path: Optional[str]
    analysis_data_path: Optional[str]
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


# ----- raw PMAI section walk (preview strips + vocals) ------------------------
# Ported from the spike PoCs (credit where the byte-exact logic was proven):
#   * PWV6 walker:  .tmp/.tmp_spike_a1_bench_dump.py::extract_pwv6_raw (SPIKE-A1)
#   * PVDI walker + regions: .tmp/.tmp_spike_b1_pvdi_regions.py (SPIKE-B1)
# pyrekordbox 0.4.4 cannot serve either tag (PWV6 tag.get() raises KeyError: 0,
# PVDI is dropped with a "not supported" warning), and its .DAT parse costs up
# to 51 ms/track (A1 gotcha 2) -- the raw walk is 0.011-0.055 ms.

def _iter_pmai_sections(buf: bytes) -> Iterator[tuple[bytes, int, int, int]]:
    """Yield ``(fourcc, offset, head_len, total_len)`` over a PMAI container."""
    if buf[:4] != b"PMAI":
        raise ValueError("not an ANLZ PMAI container")
    off = struct.unpack(">I", buf[4:8])[0]
    while off + 12 <= len(buf):
        fourcc = buf[off:off + 4]
        head_len, total_len = struct.unpack(">II", buf[off + 4:off + 12])
        if total_len <= 0:
            raise ValueError(f"corrupt ANLZ section length at offset {off}")
        yield fourcc, off, head_len, total_len
        off += total_len


def _read_pwv6_tri(path: Path) -> Optional[np.ndarray]:
    """.2EX PWV6 -> ``(n, 3)`` uint8 columns [low, mid, hi], or None if absent."""
    buf = path.read_bytes()
    for fourcc, off, head_len, _total_len in _iter_pmai_sections(buf):
        if fourcc != b"PWV6":
            continue
        entry_bytes, entries = struct.unpack(">II", buf[off + 12:off + 20])
        if entry_bytes != 3:
            raise ValueError(f"PWV6 entry size {entry_bytes} != 3 in {path}")
        return np.frombuffer(
            buf, np.uint8, entries * 3, off + head_len
        ).reshape(entries, 3)
    return None


def _read_pwv4_mono(path: Path) -> Optional[np.ndarray]:
    """.EXT PWV4 luminance byte (0..127) duplicated to 3 bands, or None.

    PWV4 is an RGB *colour* preview (6 bytes/col); its r/g/b bytes are display
    colours, not frequency bands, so mapping them onto low/mid/hi would invent
    data. Byte 0 is a real mono luminance/height envelope (verified: Pearson
    0.60 vs the PWV6 per-column height on this library) -- serve that,
    duplicated, exactly like the PWAV mono fallback.
    """
    buf = path.read_bytes()
    for fourcc, off, head_len, _total_len in _iter_pmai_sections(buf):
        if fourcc != b"PWV4":
            continue
        entry_bytes, entries = struct.unpack(">II", buf[off + 12:off + 20])
        if entry_bytes != 6:
            raise ValueError(f"PWV4 entry size {entry_bytes} != 6 in {path}")
        cols = np.frombuffer(
            buf, np.uint8, entries * 6, off + head_len
        ).reshape(entries, 6)
        lum = cols[:, _PWV4_LUMINANCE_BYTE] & 0x7F
        return np.repeat(lum[:, np.newaxis], 3, axis=1)
    return None


def _read_pwav_mono(path: Path) -> Optional[np.ndarray]:
    """.DAT PWAV heights (low 5 bits, 0..31) duplicated to 3 bands, or None."""
    buf = path.read_bytes()
    for fourcc, off, head_len, total_len in _iter_pmai_sections(buf):
        if fourcc != b"PWAV":
            continue
        entries = struct.unpack(">I", buf[off + 12:off + 16])[0]
        if entries != total_len - head_len:
            raise ValueError(
                f"PWAV length mismatch in {path}: "
                f"{entries} entries vs {total_len - head_len} payload bytes"
            )
        heights = (
            np.frombuffer(buf, np.uint8, entries, off + head_len)
            & _PWAV_HEIGHT_MASK
        )
        return np.repeat(heights[:, np.newaxis], 3, axis=1)
    return None


def _peak_downsample_cols(cols: np.ndarray, width: int) -> np.ndarray:
    """Peak-max downsample ``(n, 3)`` columns to ``(width, 3)`` (A1: max per
    bucket, not mean, so transients survive)."""
    n = cols.shape[0]
    if n < width:
        raise ValueError(
            f"cannot downsample {n} ANLZ columns to {width}: all known preview "
            f"tags carry >= 400 columns, so this is corrupt data"
        )
    edges = (np.arange(width) * n) // width
    return np.maximum.reduceat(cols, edges, axis=0)


# Fallback chain per SPIKE-A1 section 4 / SPIKE-SUMMARY: PWV6 (.2EX tri-band)
# -> PWV4 (.EXT, mono luminance) -> PWAV (.DAT, mono blue) -> (None, None).
_PREVIEW_SOURCES: tuple[tuple[str, Any], ...] = (
    (".2EX", _read_pwv6_tri),
    (".EXT", _read_pwv4_mono),
    (".DAT", _read_pwav_mono),
)


def preview_strip(
    analysis_data_path: Optional[str],
) -> tuple[Optional[str], Optional[int]]:
    """Return ``(preview_b64, preview_max)`` for one track, or ``(None, None)``.

    ``preview_b64`` encodes ``uint8[120][3]`` interleaved [low, mid, hi] per
    column, peak-downsampled from the ANLZ preview tag (contract item 1).
    ``preview_max`` is the per-track max band value -- clients normalise by it,
    never by 127 (A1 gotcha 3: observed values top out ~87).

    ``(None, None)`` is the real "no ANLZ analysis" state (no path, files
    missing, or no preview tag anywhere in the chain) -- never zeros.
    Results are cached per AnalysisDataPath and revalidated by source mtime.
    """
    if not analysis_data_path:
        return None, None
    dat = resolve_share_path(analysis_data_path)
    for suffix, reader in _PREVIEW_SOURCES:
        source = dat.with_suffix(suffix)
        if not source.is_file():
            continue
        mtime = source.stat().st_mtime
        with _PREVIEW_LOCK:
            hit = _PREVIEW_CACHE.get(analysis_data_path)
        if hit is not None and hit[0] == str(source) and hit[1] == mtime:
            return hit[2], hit[3]
        cols = reader(source)
        if cols is None:
            continue
        strip = _peak_downsample_cols(cols, PREVIEW_COLUMNS)
        b64 = base64.b64encode(strip.tobytes()).decode("ascii")
        strip_max = int(strip.max())
        with _PREVIEW_LOCK:
            _PREVIEW_CACHE[analysis_data_path] = (str(source), mtime, b64, strip_max)
        return b64, strip_max
    return None, None


# ----- vocals (PVDI -- SPIKE-B1 decode, SPIKE-B2 calibrated params) -----------

def read_pvdi(path_2ex: Path) -> Optional[tuple[float, bytes]]:
    """Return ``(fps, envelope)`` from a .2EX, or None when PVDI is absent.

    Absence is a REAL state (track analyzed pre-rekordbox-7; 61% of the
    library today) -- callers must surface it as "not analyzed", never as an
    empty bar. Malformed PVDI raises: the fixed header bytes were uniform
    across all 3985 carriers (B1 section 3), so any deviation means the
    format changed and must fail loudly, not decode garbage.
    """
    buf = path_2ex.read_bytes()
    for fourcc, off, head_len, _total_len in _iter_pmai_sections(buf):
        if fourcc != b"PVDI":
            continue
        fixed = buf[off + 12:off + 20]
        if fixed != _PVDI_FIXED_HEADER:
            raise ValueError(
                f"PVDI fixed header changed in {path_2ex}: "
                f"{fixed.hex()} != {_PVDI_FIXED_HEADER.hex()} -- format bump?"
            )
        count = struct.unpack(">I", buf[off + 20:off + 24])[0]
        envelope = buf[off + head_len:off + head_len + count]
        if len(envelope) != count:
            raise ValueError(
                f"PVDI payload truncated in {path_2ex}: "
                f"{len(envelope)} < {count} bytes"
            )
        if envelope and max(envelope) > 4:
            raise ValueError(
                f"PVDI intensity > 4 in {path_2ex}: format changed"
            )
        return _PVDI_RATE / _PVDI_HOP, envelope
    return None


def _vocal_regions(envelope: bytes, fps: float) -> list[dict[str, Any]]:
    """Runs of intensity >= VOCAL_INTENSITY_MIN, merged (< VOCAL_MERGE_GAP_S
    gaps), dropped when shorter than VOCAL_MIN_REGION_S; intensity = max in
    the merged run (SPIKE-B1 recipe + SPIKE-B2 calibrated params)."""
    runs: list[list[int]] = []
    start: Optional[int] = None
    for i, value in enumerate(envelope):
        if value >= VOCAL_INTENSITY_MIN and start is None:
            start = i
        elif value < VOCAL_INTENSITY_MIN and start is not None:
            runs.append([start, i])
            start = None
    if start is not None:
        runs.append([start, len(envelope)])

    merged: list[list[int]] = []
    for run_start, run_end in runs:
        if merged and (run_start - merged[-1][1]) / fps < VOCAL_MERGE_GAP_S:
            merged[-1][1] = run_end
        else:
            merged.append([run_start, run_end])

    regions: list[dict[str, Any]] = []
    for run_start, run_end in merged:
        if (run_end - run_start) / fps < VOCAL_MIN_REGION_S:
            continue
        regions.append({
            "start_s": round(run_start / fps, 2),
            "end_s": round(run_end / fps, 2),
            "intensity": int(max(envelope[run_start:run_end])),
        })
    return regions


def vocals_payload(path_2ex: Path) -> dict[str, Any]:
    """The ``vocals`` field for /anlz -- exactly one of the three mandatory
    states (SPIKE-SUMMARY section 3): rekordbox / no_vocals / not_analyzed."""
    if not path_2ex.is_file():
        return {"status": "not_analyzed"}
    pvdi = read_pvdi(path_2ex)
    if pvdi is None:
        return {"status": "not_analyzed"}
    fps, envelope = pvdi
    regions = _vocal_regions(envelope, fps)
    if not regions:
        return {"status": "no_vocals", "fps": round(fps, 2), "regions": []}
    return {"status": "rekordbox", "fps": round(fps, 2), "regions": regions}


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


# ----- bulk row hydration (contract items 1-4) --------------------------------

def _chunked(seq: Sequence[str], size: int = _SQL_CHUNK) -> Iterator[Sequence[str]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def bulk_rb_meta(stable_ids: Sequence[str]) -> dict[str, RbRowMeta]:
    """Bulk stable_id -> rekordbox row meta (folder/anlz/comment/genre).

    One chunked query against state.db (vendor mapping) + one against
    master.plain.db (djmdContent) instead of a per-row resolve. Missing
    state.db means no vendor mappings can exist (make_backend would be on
    InMemoryBackend) -- an empty result is the true state, not a fallback.
    A missing master.plain.db while mappings exist still fails loudly via
    :func:`_open_ro`. Ids without a live mapping/content row are absent
    from the result (real states: non-rekordbox track, deleted row).
    """
    ids = list(dict.fromkeys(stable_ids))
    if not ids or not STATE_DB.exists():
        return {}
    state = _open_ro(STATE_DB, "STATE_DB")
    try:
        vendor_by_sid: dict[str, str] = {}
        for chunk in _chunked(ids):
            placeholders = ",".join("?" * len(chunk))
            for sid, vid in state.execute(
                "SELECT stable_id, vendor_id FROM track_vendor_ids "
                f"WHERE vendor = 'rekordbox' AND stable_id IN ({placeholders})",
                tuple(chunk),
            ):
                vendor_by_sid[str(sid)] = str(vid)
    finally:
        state.close()
    if not vendor_by_sid:
        return {}

    master = _open_ro(MASTER_PLAIN_DB, "MASTER_DB")
    try:
        content_by_vid: dict[str, tuple[Any, ...]] = {}
        vendor_ids = sorted(set(vendor_by_sid.values()))
        for chunk in _chunked(vendor_ids):
            placeholders = ",".join("?" * len(chunk))
            for vid, folder, adp, comment, genre in master.execute(
                "SELECT c.ID, c.FolderPath, c.AnalysisDataPath, c.Commnt, "
                "       g.Name "
                "FROM djmdContent c "
                "LEFT JOIN djmdGenre g "
                "       ON g.ID = c.GenreID AND g.rb_local_deleted = 0 "
                f"WHERE c.ID IN ({placeholders}) AND c.rb_local_deleted = 0",
                tuple(chunk),
            ):
                content_by_vid[str(vid)] = (folder, adp, comment, genre)
    finally:
        master.close()

    out: dict[str, RbRowMeta] = {}
    for sid, vid in vendor_by_sid.items():
        content = content_by_vid.get(vid)
        if content is None:
            continue
        folder, adp, comment, genre = content
        out[sid] = RbRowMeta(
            vendor_id=vid,
            folder_path=folder or None,
            analysis_data_path=adp or None,
            comment=comment or None,
            genre=genre or None,
        )
    return out


def bulk_file_exists(paths: Iterable[str]) -> dict[str, bool]:
    """Disk-truth existence for library paths via the TTL'd stat cache.

    FR-1 item 4: never stat 8k files per request. Each unique path is
    stat'ed at most once per :data:`FILE_EXISTS_TTL_S`; within the TTL,
    repeats are dict lookups. Callers pass only real local paths
    (streaming URIs have no disk truth and must be excluded upstream).
    """
    now = time.monotonic()
    wanted = {p for p in paths if p}
    out: dict[str, bool] = {}
    stale: list[str] = []
    with _FILE_EXISTS_LOCK:
        for path in wanted:
            hit = _FILE_EXISTS_CACHE.get(path)
            if hit is not None and now - hit[0] < FILE_EXISTS_TTL_S:
                out[path] = hit[1]
            else:
                stale.append(path)
    for path in stale:
        out[path] = resolve_share_path(path).is_file()
    if stale:
        with _FILE_EXISTS_LOCK:
            for path in stale:
                _FILE_EXISTS_CACHE[path] = (now, out[path])
    return out


def bulk_availability(
    stable_ids: Sequence[str],
    state_file_paths: Mapping[str, Optional[str]],
    metas: Optional[Mapping[str, RbRowMeta]] = None,
) -> dict[str, bool]:
    """``file_exists`` per stable_id: rekordbox FolderPath when mapped,
    else the state-layer file_path; streaming URIs and missing paths are
    False. ``metas`` lets callers reuse an existing bulk_rb_meta result."""
    if metas is None:
        metas = bulk_rb_meta(stable_ids)
    folder_by_sid: dict[str, Optional[str]] = {}
    for sid in stable_ids:
        meta = metas.get(sid)
        folder_by_sid[sid] = (
            meta.folder_path if meta is not None else state_file_paths.get(sid)
        )
    exists = bulk_file_exists(
        path for path in folder_by_sid.values()
        if path and not is_streaming_path(path)
    )
    return {
        sid: bool(path) and not is_streaming_path(path) and exists[path]
        for sid, path in folder_by_sid.items()
    }


def build_track_rows(tracks: Sequence[Any]) -> list[dict[str, Any]]:
    """Hydrated track rows (contract item 4) for playlist detail + listings.

    ``tracks`` are backend ``Track`` dataclasses in the order to render
    (playlist membership order / page order). One bulk vendor lookup, one
    cached stat pass and per-row cached preview extraction replace the
    old 29x per-row GET fan-out. Field names match the shared API
    contract exactly: title, artist, key, bpm, rating, duration_ms,
    genre, comments, etag, preview_b64, preview_max, file_exists,
    is_streaming.
    """
    from .etag import compute_etag

    stable_ids = [t.stable_id for t in tracks]
    metas = bulk_rb_meta(stable_ids)
    available = bulk_availability(
        stable_ids, {t.stable_id: t.file_path for t in tracks}, metas,
    )
    rows: list[dict[str, Any]] = []
    for track in tracks:
        meta = metas.get(track.stable_id)
        folder = meta.folder_path if meta is not None else track.file_path
        preview_b64, preview_max = (
            preview_strip(meta.analysis_data_path)
            if meta is not None else (None, None)
        )
        rows.append({
            "stable_id": track.stable_id,
            "title": track.title,
            "artist": track.artist,
            "key": track.key,
            "bpm": track.bpm,
            "rating": track.rating,
            "duration_ms": track.duration_ms,
            "genre": meta.genre if meta is not None else None,
            "comments": meta.comment if meta is not None else None,
            "etag": compute_etag(track.stable_id, track.updated_at),
            "preview_b64": preview_b64,
            "preview_max": preview_max,
            "file_exists": available[track.stable_id],
            "is_streaming": is_streaming_path(folder),
        })
    return rows


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
    if (
        cached.get("schema") == ANLZ_CACHE_SCHEMA
        and cached.get("anlz_mtime") == anlz_mtime
        and cached.get("points") == points
    ):
        return cached["payload"]
    return None


def build_anlz_payload(content: RbContent, points: int) -> dict[str, Any]:
    """Parse ANLZ + djmdCue into the COMPONENT-MAP 2.3 JSON, with file cache.

    Cache: data/state/anlz-cache/{stable_id}.json keyed on (schema version,
    anlz file mtime, points); any mismatch recomputes and rewrites, so old
    unversioned or stale-schema entries self-heal. Cues are always overlaid from
    the live ``djmdCue`` rows because they can change without touching ANLZ files.
    """
    directory = anlz_dir(content)
    # anlz_dir() enforced a non-None AnalysisDataPath pointing at the .DAT;
    # the .2EX sibling (PVDI carrier) shares its stem.
    assert content.analysis_data_path is not None
    twoex_path = resolve_share_path(content.analysis_data_path).with_suffix(".2EX")
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
        # contract item 5: PVDI-derived vocal regions, three explicit states.
        "vocals": vocals_payload(twoex_path),
    }

    ANLZ_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(content.stable_id).write_text(
        json.dumps({
            "schema": ANLZ_CACHE_SCHEMA,
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
    "ANLZ_CACHE_SCHEMA",
    "ARTWORK_FILENAMES",
    "AUDIO_MEDIA_TYPES",
    "FILE_EXISTS_TTL_S",
    "HOT_CUE_SLOTS",
    "MASTER_PLAIN_DB",
    "PREVIEW_COLUMNS",
    "RbContent",
    "RbRowMeta",
    "SHARE_ROOT",
    "STREAMING_PREFIXES",
    "VOCAL_INTENSITY_MIN",
    "VOCAL_MERGE_GAP_S",
    "VOCAL_MIN_REGION_S",
    "anlz_dir",
    "artwork_file",
    "audio_file",
    "build_anlz_payload",
    "build_track_rows",
    "bulk_availability",
    "bulk_file_exists",
    "bulk_rb_meta",
    "count_cues",
    "fetch_cues",
    "is_streaming_path",
    "not_found",
    "preview_strip",
    "read_pvdi",
    "resolve_content",
    "resolve_share_path",
    "vocals_payload",
]
