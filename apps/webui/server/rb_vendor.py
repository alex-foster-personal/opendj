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
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import struct
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

import numpy as np
from fastapi import HTTPException

from apps.shared import audio_quality, fs_residency, platform_paths
from apps.shared.paths import DATA_DIR as _PATHS_DATA_DIR
from apps.shared.platform_paths import (
    MappedPath,
)
from apps.shared.platform_paths import (
    resolve_library_path as resolve_library_path,
)
from apps.shared.state import locations as track_locations
from apps.vocals import cache as vocal_cache
from apps.webui.server import beatgrid_diagnostics

log = logging.getLogger(__name__)

# MDT_DATA_DIR: explicit override so a backend run against an unpacked
# scripts/data_snapshot.py pack (e.g. on a Windows box, or any Mac dev dir
# other than the default) never touches apps.shared.paths' hardcoded layout.
# Read once at import; every constant below is rederived from the SAME
# DATA_DIR so there is one source of truth. Constant NAMES are unchanged.
_MDT_DATA_DIR_ENV: Optional[str] = os.environ.get("MDT_DATA_DIR")
DATA_DIR: Path = Path(_MDT_DATA_DIR_ENV) if _MDT_DATA_DIR_ENV else _PATHS_DATA_DIR

MASTER_PLAIN_DB: Path = DATA_DIR / "master.plain.db"
ANLZ_CACHE_DIR: Path = DATA_DIR / "state" / "anlz-cache"
# Tiny sidecar cache, DELIBERATELY separate from ANLZ_CACHE_DIR: that cache's
# payload also carries the waveform bands (100s of KB - 1MB per track), which
# is fine for the rare full /anlz fetch but far too heavy to read once per
# row as the library browser's TrackTable scrolls thousands of rows into
# view. Each entry here is a few hundred bytes (one severity + one beat's
# numbers), so GET /rb-meta's existing lazy per-row fetch (see
# routes/rb_assets.get_track_rb_meta) can read it as cheaply as the
# analysis_available file-existence stat it already does.
BEATGRID_ISSUE_CACHE_DIR: Path = DATA_DIR / "state" / "beatgrid-issue-cache"
# demucs gap-fill cache written by ``python -m apps.vocals`` (SPIKE-B2).
VOCAL_CACHE_DIR: Path = vocal_cache.cache_dir(DATA_DIR)
# Matches apps.shared.paths' STATE_DIR/STATE_DB formula exactly, so this
# equals the default STATE_DB when MDT_DATA_DIR is unset.
STATE_DB: Path = DATA_DIR / "state" / "state.db"

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
# Bump whenever beatgrid_diagnostics' output shape or thresholds change.
BEATGRID_ISSUE_CACHE_SCHEMA: int = 1

# --- preview strip (SPIKE-A1 / SPIKE-SUMMARY section 2) ---
PREVIEW_COLUMNS: int = 120  # 1200 -> 120 peak-max downsample (10:1)
_PWV4_LUMINANCE_BYTE: int = 0  # verified: corr 0.60 vs PWV6 height, 0..127
_PWAV_HEIGHT_MASK: int = 0x1F  # low 5 bits = height 0..31 (A1 tag table)

# --- vocals (SPIKE-B1 / SPIKE-B2 calibrated params) ---
VOCAL_INTENSITY_MIN: int = 1  # PVDI frame value (0..4) counted as vocal
VOCAL_MERGE_GAP_S: float = 1.5  # merge regions separated by < this gap
VOCAL_MIN_REGION_S: float = 1.0  # drop merged regions shorter than this
# PVDI fixed header bytes at section offset 12..20: u16 reserved=0x0000,
# u16 hop=1024, u16 rate=22050, u16 version=1. Uniform across all 3985
# carriers (B1 section 3); any deviation means the format changed.
_PVDI_FIXED_HEADER: bytes = bytes.fromhex("0000040056220001")
_PVDI_HOP: int = 1024
_PVDI_RATE: int = 22050

# --- file-existence + preview caches ---
# file_exists is disk truth (FR-1 item 4): playable local bytes, not merely
# an inode. iCloud dataless stubs (st_size > 0, st_blocks == 0) count as
# missing -- see apps.shared.fs_residency. Per-path results are cached for
# FILE_EXISTS_TTL_S so a listing request never stats 8k files -- one bulk
# stat pass warms the cache, then repeats are dict lookups until the TTL
# lapses (30 s keeps "file restored by reconcile" visible quickly).
# The cache holds materialised st_size (None = missing or stub), so the
# SAME pass that answers file_exists also feeds audio_quality.classify.
FILE_EXISTS_TTL_S: float = 30.0
_FILE_EXISTS_LOCK = threading.Lock()
_FILE_EXISTS_CACHE: dict[str, tuple[float, Optional[int]]] = {}

# Preview strips are immutable per (source file, mtime): cache the encoded
# strip per AnalysisDataPath and revalidate with a single stat per hit.
# ~10k entries x ~500 B is a few MB; no eviction needed.
_PREVIEW_LOCK = threading.Lock()
_PREVIEW_CACHE: dict[str, tuple[str, float, str, int]] = {}

# Windows does not allow os.replace() while another thread has the destination
# open. Stage writes concurrently, then serialize reads and atomic publication
# per cache entry so unrelated tracks remain independent.
_ANLZ_CACHE_LOCKS_GUARD = threading.Lock()
_ANLZ_CACHE_LOCKS: dict[Path, threading.Lock] = {}

_SQL_CHUNK: int = 500  # keep IN (...) under SQLite's var cap


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
    play_count: int


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
    """Backward-compat alias over :func:`resolve_asset_path`.

    Kept ONLY for the legacy non-owned call sites that still expect a bare
    ``Path`` back (``apps/webui/server/routes/rb_assets.py``,
    ``tests/test_rb_assets.py``) -- new code in this module calls
    :func:`resolve_asset_path` directly so it can see the explicit
    unmapped state. Mirrors the pre-portability behaviour when the shared
    resolver reports "unmapped" by falling back to ``Path(path)`` verbatim
    (never existing -- every caller already guards with ``.is_file()``).
    """
    mapped = resolve_asset_path(path)
    if mapped.resolved is not None:
        return mapped.resolved
    if mapped.reason.startswith("unsafe:"):
        raise ValueError(f"unsafe rekordbox asset path: {path!r}")
    return Path(path)


def resolve_asset_path(path: str) -> MappedPath:
    """Map one vendor asset path and enforce symlink-aware containment."""
    return platform_paths.resolve_asset_path(path)


def _asset_sibling(mapped: MappedPath, candidate: Path) -> MappedPath:
    """Contain a derived sibling of an already-mapped vendor asset path."""
    return platform_paths.resolve_asset_sibling(mapped, candidate)


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
    mapped = resolve_asset_path(content.folder_path)
    if mapped.resolved is None:
        raise not_found(
            "AUDIO_FILE_MISSING",
            f"audio file for track {content.stable_id} could not be "
            f"resolved on this platform ({mapped.reason}): "
            f"{content.folder_path}",
        )
    path = mapped.resolved
    if not fs_residency.is_materialised(path):
        raise not_found(
            "AUDIO_FILE_MISSING",
            f"audio file for track {content.stable_id} is missing or not "
            f"materialised (dataless/iCloud stub): {path}",
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


def local_audio_file(stable_id: str) -> tuple[Path, str]:
    """Fallback audio resolution for tracks with NO rekordbox vendor mapping.

    Locally imported files (e.g. vocal stems added straight into state.db)
    have a real ``tracks.file_path`` but no djmdContent row, so
    :func:`resolve_content` 404s with ``VENDOR_MAPPING_NOT_FOUND``. The audio
    route falls back here to stream that path directly. Same residency +
    media-type gates as :func:`audio_file` -- never a mocked or missing file.
    """
    state = _open_ro(STATE_DB, "STATE_DB")
    try:
        row = state.execute(
            "SELECT file_path FROM tracks WHERE stable_id = ?", (stable_id,)
        ).fetchone()
    finally:
        state.close()
    if row is None:
        raise not_found("TRACK_NOT_FOUND", f"unknown stable_id {stable_id}")
    if not row[0]:
        raise not_found(
            "AUDIO_FILE_MISSING",
            f"track {stable_id} has no rekordbox mapping and no file_path",
        )
    mapped = resolve_asset_path(row[0])
    if mapped.resolved is None:
        raise not_found(
            "AUDIO_FILE_MISSING",
            f"file_path for track {stable_id} could not be resolved "
            f"on this platform ({mapped.reason}): {row[0]}",
        )
    path = mapped.resolved
    if not fs_residency.is_materialised(path):
        raise not_found(
            "AUDIO_FILE_MISSING",
            f"file_path for track {stable_id} is missing or not "
            f"materialised (dataless/iCloud stub): {path}",
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


def resolve_playable_audio(
    stable_id: str, *, share: bool = False
) -> track_locations.PickedAudio:
    """Pick the single file the frontend may play. Never returns a list."""
    folder_path: Optional[str] = None
    try:
        content = resolve_content(stable_id)
        folder_path = content.folder_path
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") == "TRACK_NOT_FOUND":
            raise
        if detail.get("code") != "VENDOR_MAPPING_NOT_FOUND":
            raise
    from apps.shared.crate_index import resolve_crate_audio

    crate_audio = resolve_crate_audio(stable_id)
    extra_paths: tuple[tuple[str, str, track_locations.Kind], ...] = (
        ((str(crate_audio), "crate-index", "local"),) if crate_audio is not None else ()
    )
    state = _open_ro(STATE_DB, "STATE_DB")
    try:
        exists = state.execute(
            "SELECT 1 FROM tracks WHERE stable_id = ?", (stable_id,)
        ).fetchone()
        if exists is None:
            raise not_found("TRACK_NOT_FOUND", f"unknown stable_id {stable_id}")
        picked = track_locations.pick_playable(
            state,
            stable_id,
            policy=track_locations.policy_from_env(share=share),
            folder_path=folder_path,
            extra_paths=extra_paths,
        )
    finally:
        state.close()
    if picked is None:
        raise not_found(
            "AUDIO_FILE_MISSING",
            f"no working audio location for track {stable_id}",
        )
    return picked


def empty_anlz_payload(stable_id: str, points: int) -> dict[str, Any]:
    """Valid, empty ``/anlz`` payload for tracks with NO rekordbox analysis
    (locally imported files, e.g. vocal stems). Shapes mirror
    :func:`build_anlz_payload` exactly -- every array empty, ``vocals``
    not_analyzed -- so a deck can load and play with no grid/waveform rather
    than the whole load failing on a 404. Never synthesised data.
    """
    empty_bands = {"length": 0, "low": [], "mid": [], "high": []}
    return {
        "stable_id": stable_id,
        "points": points,
        "waveform": {
            "kind": "mono",
            "preview": dict(empty_bands),
            "detail": dict(empty_bands),
        },
        "beatgrid": {"beat_count": 0, "beats": []},
        "cues": [],
        "phrases": [],
        "vocals": {"status": "not_analyzed"},
    }


def empty_hot_cue_slots() -> list[dict[str, Any]]:
    """Eight empty hot-cue slots for tracks with no rekordbox mapping.

    Deterministic per-slot revisions keep the shape stable across calls; these
    tracks have no djmdContent row, so cue writeback is unsupported anyway (a
    PUT would 404), and the deck-load path only needs a non-null slot list.
    """
    return [
        {
            "slot": slot,
            "cue": None,
            "revision": hashlib.sha256(
                f"empty-hot-cue:{slot}".encode("utf-8")
            ).hexdigest(),
        }
        for slot in HOT_CUE_SLOTS
    ]


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
    mapped = resolve_asset_path(content.image_path)
    if mapped.resolved is None:
        raise not_found(
            "ARTWORK_NOT_FOUND",
            f"artwork ImagePath for track {content.stable_id} could not be "
            f"resolved on this platform ({mapped.reason}): "
            f"{content.image_path}",
        )
    derived = _asset_sibling(mapped, mapped.resolved.parent / ARTWORK_FILENAMES[size])
    if derived.resolved is None:
        raise not_found(
            "ARTWORK_NOT_FOUND",
            f"artwork file for track {content.stable_id} is unsafe "
            f"({derived.reason}): {content.image_path}",
        )
    path = derived.resolved
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
    mapped = resolve_asset_path(content.analysis_data_path)
    if mapped.resolved is None:
        raise not_found(
            "ANALYSIS_NOT_FOUND",
            f"AnalysisDataPath for track {content.stable_id} could not be "
            f"resolved on this platform ({mapped.reason}): "
            f"{content.analysis_data_path}",
        )
    dat = mapped.resolved
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
        fourcc = buf[off : off + 4]
        head_len, total_len = struct.unpack(">II", buf[off + 4 : off + 12])
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
        entry_bytes, entries = struct.unpack(">II", buf[off + 12 : off + 20])
        if entry_bytes != 3:
            raise ValueError(f"PWV6 entry size {entry_bytes} != 3 in {path}")
        return np.frombuffer(buf, np.uint8, entries * 3, off + head_len).reshape(
            entries, 3
        )
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
        entry_bytes, entries = struct.unpack(">II", buf[off + 12 : off + 20])
        if entry_bytes != 6:
            raise ValueError(f"PWV4 entry size {entry_bytes} != 6 in {path}")
        cols = np.frombuffer(buf, np.uint8, entries * 6, off + head_len).reshape(
            entries, 6
        )
        lum = cols[:, _PWV4_LUMINANCE_BYTE] & 0x7F
        return np.repeat(lum[:, np.newaxis], 3, axis=1)
    return None


def _read_pwav_mono(path: Path) -> Optional[np.ndarray]:
    """.DAT PWAV heights (low 5 bits, 0..31) duplicated to 3 bands, or None."""
    buf = path.read_bytes()
    for fourcc, off, head_len, total_len in _iter_pmai_sections(buf):
        if fourcc != b"PWAV":
            continue
        entries = struct.unpack(">I", buf[off + 12 : off + 16])[0]
        if entries != total_len - head_len:
            raise ValueError(
                f"PWAV length mismatch in {path}: "
                f"{entries} entries vs {total_len - head_len} payload bytes"
            )
        heights = (
            np.frombuffer(buf, np.uint8, entries, off + head_len) & _PWAV_HEIGHT_MASK
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
    mapped = resolve_asset_path(analysis_data_path)
    if mapped.resolved is None:
        return None, None  # unmapped on this platform: a real "no analysis" state
    dat = mapped.resolved
    for suffix, reader in _PREVIEW_SOURCES:
        derived = _asset_sibling(mapped, dat.with_suffix(suffix))
        if derived.resolved is None:
            return None, None
        source = derived.resolved
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
        fixed = buf[off + 12 : off + 20]
        if fixed != _PVDI_FIXED_HEADER:
            raise ValueError(
                f"PVDI fixed header changed in {path_2ex}: "
                f"{fixed.hex()} != {_PVDI_FIXED_HEADER.hex()} -- format bump?"
            )
        count = struct.unpack(">I", buf[off + 20 : off + 24])[0]
        envelope = buf[off + head_len : off + head_len + count]
        if len(envelope) != count:
            raise ValueError(
                f"PVDI payload truncated in {path_2ex}: {len(envelope)} < {count} bytes"
            )
        if envelope and max(envelope) > 4:
            raise ValueError(f"PVDI intensity > 4 in {path_2ex}: format changed")
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
        regions.append(
            {
                "start_s": round(run_start / fps, 2),
                "end_s": round(run_end / fps, 2),
                "intensity": int(max(envelope[run_start:run_end])),
            }
        )
    return regions


def vocals_payload(path_2ex: Path) -> dict[str, Any]:
    """The PVDI-derived ``vocals`` field for /anlz -- exactly one of the
    three PVDI states (SPIKE-SUMMARY section 3): rekordbox / no_vocals /
    not_analyzed. The demucs fallback (fourth status) is merged at serve
    time by :func:`merge_demucs_vocals`, never cached here."""
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


def demucs_vocals_payload(content: RbContent) -> Optional[dict[str, Any]]:
    """``{"status": "demucs", ...}`` from the vocal-cache, or None.

    None covers every real absence: no cache entry, schema-bumped entry,
    streaming/pathless track, audio gone from disk, or audio_mtime changed
    since analysis (regions computed for a different file must never be
    served - SPIKE-SUMMARY section 3 cache contract). Corrupt entries
    raise inside apps.vocals.cache - fail fast, no invented regions.
    """
    if content.folder_path is None or is_streaming_path(content.folder_path):
        return None
    mapped = resolve_asset_path(content.folder_path)
    if mapped.resolved is None:
        return None  # unmapped on this platform: audio is not locatable
    entry = vocal_cache.load_valid_entry(
        VOCAL_CACHE_DIR / f"{content.stable_id}.json",
        mapped.resolved,
    )
    if entry is None:
        return None
    return vocal_cache.anlz_vocals_of(entry)


def merge_demucs_vocals(payload: dict[str, Any], content: RbContent) -> dict[str, Any]:
    """Serve-time merge: when PVDI said not_analyzed, consult the demucs
    vocal-cache. Applied AFTER the anlz file cache on purpose -- the cached
    payload stays PVDI-only, so a vocal-cache entry landing (or being
    invalidated) later is reflected without an anlz-cache schema bump.
    After this merge, ``not_analyzed`` means NEITHER source exists."""
    if payload["vocals"]["status"] != "not_analyzed":
        return payload
    demucs = demucs_vocals_payload(content)
    if demucs is None:
        return payload
    return {**payload, "vocals": demucs}


def vocals_for_content(content: RbContent) -> dict[str, Any]:
    """Listing hot path: PVDI from .2EX, else demucs vocal-cache.

    Same four statuses as ``/anlz`` vocals. Used by :func:`build_track_rows`
    so library PreviewStrip blue bars do not need a per-row /anlz fetch.
    """
    path_2ex: Optional[Path] = None
    if content.analysis_data_path:
        mapped = resolve_asset_path(content.analysis_data_path)
        if mapped.resolved is not None:
            mapped_twoex = _asset_sibling(mapped, mapped.resolved.with_suffix(".2EX"))
            if mapped_twoex.resolved is not None:
                path_2ex = mapped_twoex.resolved
    vocals = (
        vocals_payload(path_2ex) if path_2ex is not None else {"status": "not_analyzed"}
    )
    if vocals["status"] != "not_analyzed":
        return vocals
    demucs = demucs_vocals_payload(content)
    return demucs if demucs is not None else vocals


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
            "SELECT ID, Kind, InMsec, InFrame, InMpegFrame, InMpegAbs, "
            "       OutMsec, OutFrame, ActiveLoop, BeatLoopSize, "
            "       ColorTableIndex, Comment, updated_at "
            "FROM djmdCue WHERE ContentID = ? AND rb_local_deleted = 0",
            (vendor_id,),
        ).fetchall()
    finally:
        master.close()

    cues: list[dict[str, Any]] = []
    for row in rows:
        snapshot = _cue_snapshot_from_row(row)
        kind_i = snapshot["kind"]
        if kind_i is None or not 0 <= int(kind_i) <= 8:
            continue  # Kind 9-11 excluded v1: slot mapping unverified (PARITY-TODO)
        in_ms = snapshot["in_ms"]
        out_ms = snapshot["out_ms"]
        active_loop = snapshot["active_loop"]
        loop_size = snapshot["beat_loop_size"]
        color_idx = snapshot["color_table_index"]
        comment = snapshot["comment"]
        is_loop = bool(out_ms and int(out_ms) > 0)
        if int(kind_i) == 0:
            kind = "loop" if is_loop else "memory"
            slot: Optional[str] = None
        else:
            kind = "hot_cue"
            slot = HOT_CUE_SLOTS[int(kind_i) - 1]
        cues.append(
            {
                "kind": kind,
                "slot": slot,
                "in_ms": int(in_ms) if in_ms is not None else None,
                "out_ms": int(out_ms) if is_loop else None,
                "is_loop": is_loop,
                "active_loop": bool(active_loop),
                "beat_loop_size": int(loop_size) if loop_size is not None else None,
                "color_table_index": int(color_idx) if color_idx is not None else None,
                "comment": comment or None,
            }
        )
    cues.sort(key=lambda c: c["in_ms"] if c["in_ms"] is not None else -1)
    return cues


def count_cues(vendor_id: str) -> int:
    """All live djmdCue rows for the track (incl. Kind 9-11, for rb-meta)."""
    master = _open_ro(MASTER_PLAIN_DB, "MASTER_DB")
    try:
        row = master.execute(
            "SELECT COUNT(*) FROM djmdCue WHERE ContentID = ? AND rb_local_deleted = 0",
            (vendor_id,),
        ).fetchone()
        return int(row[0])
    finally:
        master.close()


# ----- bulk row hydration (contract items 1-4) --------------------------------


def _chunked(seq: Sequence[str], size: int = _SQL_CHUNK) -> Iterator[Sequence[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


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
            for vid, folder, adp, comment, genre, play_count in master.execute(
                "SELECT c.ID, c.FolderPath, c.AnalysisDataPath, c.Commnt, "
                "       g.Name, c.DJPlayCount "
                "FROM djmdContent c "
                "LEFT JOIN djmdGenre g "
                "       ON g.ID = c.GenreID AND g.rb_local_deleted = 0 "
                f"WHERE c.ID IN ({placeholders}) AND c.rb_local_deleted = 0",
                tuple(chunk),
            ):
                content_by_vid[str(vid)] = (folder, adp, comment, genre, play_count)
    finally:
        master.close()

    out: dict[str, RbRowMeta] = {}
    for sid, vid in vendor_by_sid.items():
        content = content_by_vid.get(vid)
        if content is None:
            continue
        folder, adp, comment, genre, play_count = content
        out[sid] = RbRowMeta(
            vendor_id=vid,
            folder_path=folder or None,
            analysis_data_path=adp or None,
            comment=comment or None,
            genre=genre or None,
            play_count=int(play_count or 0),
        )
    return out


def bulk_file_size(paths: Iterable[str]) -> dict[str, Optional[int]]:
    """Disk-truth size in bytes per library path (None = not on disk).

    FR-1 item 4: never stat 8k files per request. Each unique path is
    stat'ed at most once per :data:`FILE_EXISTS_TTL_S`; within the TTL,
    repeats are dict lookups. Callers pass only real local paths
    (streaming URIs have no disk truth and must be excluded upstream).
    """
    now = time.monotonic()
    wanted = {p for p in paths if p}
    out: dict[str, Optional[int]] = {}
    stale: list[str] = []
    with _FILE_EXISTS_LOCK:
        for path in wanted:
            hit = _FILE_EXISTS_CACHE.get(path)
            if hit is not None and now - hit[0] < FILE_EXISTS_TTL_S:
                out[path] = hit[1]
            else:
                stale.append(path)
    for path in stale:
        mapped = resolve_asset_path(path)
        out[path] = _stat_size(mapped.resolved)
    if stale:
        with _FILE_EXISTS_LOCK:
            for path in stale:
                _FILE_EXISTS_CACHE[path] = (now, out[path])
    return out


def _stat_size(resolved: Optional[Path]) -> Optional[int]:
    """Materialised st_size, or None when missing / not a file / dataless stub."""
    if resolved is None:
        return None
    return fs_residency.materialised_size(resolved)


def bulk_file_exists(paths: Iterable[str]) -> dict[str, bool]:
    """Disk-truth existence per path, derived from the same cached stat."""
    return {p: size is not None for p, size in bulk_file_size(paths).items()}


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
        path for path in folder_by_sid.values() if path and not is_streaming_path(path)
    )
    loc_paths: dict[str, list[str]] = {sid: [] for sid in stable_ids}
    if STATE_DB.exists():
        state = _open_ro(STATE_DB, "STATE_DB")
        try:
            loc_paths = track_locations.list_location_paths(state, list(stable_ids))
        finally:
            state.close()
    extra = [p for paths in loc_paths.values() for p in paths if p]
    extra_exists = bulk_file_exists(extra) if extra else {}
    return {
        sid: (
            bool(path)
            and not is_streaming_path(path)
            and exists.get(path, False)
        )
        or any(extra_exists.get(p) for p in loc_paths.get(sid, []))
        for sid, path in folder_by_sid.items()
    }


def bulk_quality(
    stable_ids: Sequence[str],
    folder_by_sid: Mapping[str, Optional[str]],
    duration_ms_by_sid: Mapping[str, Optional[int]],
) -> dict[str, dict]:
    """Venue-rung quality dict per stable_id (apps.shared.audio_quality).

    Sizes come from :func:`bulk_file_size`, i.e. the SAME TTL'd stat pass
    that already answers file_exists -- so a 1000-row listing pays no extra
    stat for the badge. Streaming URIs and missing files get an honest
    UNKNOWN rather than a guessed rung.
    """
    sizes = bulk_file_size(
        path for path in folder_by_sid.values() if path and not is_streaming_path(path)
    )
    out: dict[str, dict] = {}
    for sid in stable_ids:
        path = folder_by_sid.get(sid)
        if not path:
            out[sid] = audio_quality.classify(None, None).as_dict()
        elif is_streaming_path(path):
            out[sid] = audio_quality.Quality(
                None, None, "", False, "streaming track, no local file"
            ).as_dict()
        else:
            size = sizes.get(path)
            if size is None:
                out[sid] = audio_quality.Quality(
                    None, None, Path(path).suffix.lower(), False, "file missing"
                ).as_dict()
            else:
                out[sid] = audio_quality.classify(
                    path, duration_ms_by_sid.get(sid), size
                ).as_dict()
    return out


def build_track_rows(tracks: Sequence[Any]) -> list[dict[str, Any]]:
    """Hydrated track rows (contract item 4) for playlist detail + listings.

    ``tracks`` are backend ``Track`` dataclasses in the order to render
    (playlist membership order / page order). One bulk vendor lookup, one
    cached stat pass and per-row cached preview extraction replace the
    old 29x per-row GET fan-out. Field names match the shared API
    contract exactly: title, artist, key, bpm, rating, duration_ms,
    genre, comments, etag, preview_b64, preview_max, file_exists,
    is_streaming, quality, play_count, vocals, stems.
    """
    from .etag import compute_etag
    from .stem_artifacts import bulk_stem_summaries

    stable_ids = [t.stable_id for t in tracks]
    metas = bulk_rb_meta(stable_ids)
    available = bulk_availability(
        stable_ids,
        {t.stable_id: t.file_path for t in tracks},
        metas,
    )
    folder_by_sid = {
        t.stable_id: (
            metas[t.stable_id].folder_path
            if metas.get(t.stable_id) is not None
            else t.file_path
        )
        for t in tracks
    }
    quality = bulk_quality(
        stable_ids,
        folder_by_sid,
        {t.stable_id: t.duration_ms for t in tracks},
    )
    stems = bulk_stem_summaries(stable_ids)
    rows: list[dict[str, Any]] = []
    for track in tracks:
        meta = metas.get(track.stable_id)
        folder = meta.folder_path if meta is not None else track.file_path
        preview_b64, preview_max = (
            preview_strip(meta.analysis_data_path) if meta is not None else (None, None)
        )
        content = RbContent(
            stable_id=track.stable_id,
            vendor_id=meta.vendor_id if meta is not None else "",
            folder_path=folder,
            image_path=None,
            analysis_data_path=(meta.analysis_data_path if meta is not None else None),
            length_s=None,
            comment=None,
            genre=None,
        )
        rows.append(
            {
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
                "quality": quality[track.stable_id],
                "play_count": meta.play_count if meta is not None else 0,
                "vocals": vocals_for_content(content),
                "stems": stems[track.stable_id],
            }
        )
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


def _first_tags(directory: Path) -> tuple[dict[str, Any], list[str]]:
    """First occurrence of each tag type, plus the files that would not parse.

    Parses each ANLZ file SEPARATELY. ``read_anlz_files`` parses the set in one
    call, so a single unparseable sibling took the whole track's analysis down
    with a 500 -- and rekordbox does write files pyrekordbox cannot read: the
    100 acapellas imported Sat 8 Aug 2026 have an ANLZ0000.EXT whose colour
    waveform tag fails a construct const check, while their .DAT (PQTZ
    beatgrid, PWAV, PCOB cues) and .2EX (PWV6/PWV7 tri-band) parse perfectly.

    Losing one file is NOT the same as losing the analysis, so the parseable
    files are kept -- but the failure is RETURNED, never swallowed. The caller
    puts it in the payload so a client can say which lanes are missing instead
    of showing an empty waveform lane that looks like real silence.
    """
    from pyrekordbox.anlz import AnlzFile

    tags: dict[str, Any] = {}
    unreadable: list[str] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.upper() not in {".DAT", ".EXT", ".2EX"}:
            continue
        try:
            anlz_file = AnlzFile.parse_file(str(path))
        except Exception as exc:
            log.warning("ANLZ file %s is unparseable (%s)", path.name, exc)
            unreadable.append(path.name)
            continue
        for tag in anlz_file.tags:  # tags is a LIST in pyrekordbox 0.4.4
            tags.setdefault(tag.type, tag)
    if not tags:
        raise not_found(
            "ANALYSIS_NOT_FOUND",
            f"no ANLZ file in {directory} could be parsed: {unreadable}",
        )
    return tags, unreadable


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
        phrases.append(
            {
                "start_s": time_of_beat(start_beat),
                "end_s": time_of_beat(stop_beat),
                "kind": int(entry.kind),
                "mood": mood,
            }
        )
    return phrases


def _anlz_mtime(directory: Path) -> float:
    files = sorted(directory.glob("ANLZ*"))
    if not files:
        raise not_found("ANALYSIS_NOT_FOUND", f"no ANLZ files in directory {directory}")
    return max(f.stat().st_mtime for f in files)


def _cache_path(stable_id: str) -> Path:
    return ANLZ_CACHE_DIR / f"{stable_id}.json"


def _cache_lock(path: Path) -> threading.Lock:
    with _ANLZ_CACHE_LOCKS_GUARD:
        lock = _ANLZ_CACHE_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _ANLZ_CACHE_LOCKS[path] = lock
        return lock


def _load_cached_payload(
    stable_id: str, anlz_mtime: float, points: int
) -> Optional[dict[str, Any]]:
    path = _cache_path(stable_id)
    with _cache_lock(path):
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


def _store_cached_payload(
    stable_id: str, anlz_mtime: float, points: int, payload: dict[str, Any]
) -> None:
    """Persist a cache entry atomically through a unique sibling tempfile.

    A crash mid-write must never leave a truncated {stable_id}.json behind:
    the entry lands in a unique sibling tempfile first and only ``os.replace``
    publishes it, so concurrent readers and writers see a complete entry.
    """
    ANLZ_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(stable_id)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(
            json.dumps(
                {
                    "schema": ANLZ_CACHE_SCHEMA,
                    "anlz_mtime": anlz_mtime,
                    "points": points,
                    "payload": payload,
                }
            ),
            encoding="utf-8",
        )
        with _cache_lock(path):
            os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ----- beatgrid data-quality diagnostic (browser Err column) ----------------
# Perf tradeoff (see BEATGRID_ISSUE_CACHE_DIR above): the diagnostic is only
# ever COMPUTED here, piggybacked on a full ANLZ parse that already happened
# for another reason (deck load, waveform view, RunAnalyses). Bulk row
# listings (build_track_rows) and the browser's lazy per-row rb-meta fetch
# never parse PQTZ themselves - rb-meta only READS the tiny cached verdict
# via cached_beatgrid_issue(), so a track shows no Err dot until its full
# ANLZ has actually been fetched once. That is an honest "not yet evaluated"
# state, not a wrong answer, and it self-heals the first time anything reads
# that track's /anlz.


def _beatgrid_issue_cache_path(stable_id: str) -> Path:
    return BEATGRID_ISSUE_CACHE_DIR / f"{stable_id}.json"


def _read_beatgrid_issue_cache_entry(stable_id: str) -> Optional[dict[str, Any]]:
    path = _beatgrid_issue_cache_path(stable_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("beatgrid-issue cache unreadable, ignoring: %s (%s)", path, exc)
        return None


def _store_beatgrid_issue_cache(
    stable_id: str, dat_mtime: float, issue: Optional[dict[str, Any]]
) -> None:
    """Atomic write, same tempfile-then-replace pattern as _store_cached_payload."""
    BEATGRID_ISSUE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _beatgrid_issue_cache_path(stable_id)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(
            json.dumps(
                {
                    "schema": BEATGRID_ISSUE_CACHE_SCHEMA,
                    "dat_mtime": dat_mtime,
                    "issue": issue,
                }
            ),
            encoding="utf-8",
        )
        with _cache_lock(path):
            os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _ensure_beatgrid_issue_cached(
    stable_id: str, dat_mtime: float, beats: Sequence[Mapping[str, Any]]
) -> None:
    """Compute + persist the diagnostic only if the sidecar is missing or
    stale for this exact AnalysisDataPath mtime; a fresh match is a no-op so
    a warm anlz-cache hit stays cheap on every subsequent /anlz call."""
    entry = _read_beatgrid_issue_cache_entry(stable_id)
    if (
        entry is not None
        and entry.get("schema") == BEATGRID_ISSUE_CACHE_SCHEMA
        and entry.get("dat_mtime") == dat_mtime
    ):
        return
    issue = beatgrid_diagnostics.detect_beatgrid_issue(beats)
    _store_beatgrid_issue_cache(stable_id, dat_mtime, issue)


def cached_beatgrid_issue(content: RbContent) -> Optional[dict[str, Any]]:
    """Cheap read-only lookup for GET /rb-meta - see the module comment above
    for the full perf rationale. None means either "no issue" or "never
    evaluated yet"; both are honest and this never fabricates a verdict."""
    if content.analysis_data_path is None:
        return None
    mapped = resolve_asset_path(content.analysis_data_path)
    if mapped.resolved is None:
        return None
    try:
        dat_mtime = mapped.resolved.stat().st_mtime
    except OSError:
        return None
    entry = _read_beatgrid_issue_cache_entry(content.stable_id)
    if (
        entry is None
        or entry.get("schema") != BEATGRID_ISSUE_CACHE_SCHEMA
        or entry.get("dat_mtime") != dat_mtime
    ):
        return None
    return entry.get("issue")


def build_anlz_payload(content: RbContent, points: int) -> dict[str, Any]:
    """Parse ANLZ + djmdCue into the COMPONENT-MAP 2.3 JSON, with file cache.

    Cache: data/state/anlz-cache/{stable_id}.json keyed on (schema version,
    anlz file mtime, points); any mismatch recomputes and rewrites, so old
    unversioned or stale-schema entries self-heal. Cues are always overlaid from
    the live ``djmdCue`` rows because they can change without touching ANLZ files.
    """
    directory = anlz_dir(content)
    # anlz_dir() already resolved + verified AnalysisDataPath (mapped and
    # on disk); the .2EX sibling (PVDI carrier) shares its stem.
    assert content.analysis_data_path is not None
    mapped_adp = resolve_asset_path(content.analysis_data_path)
    assert mapped_adp.resolved is not None  # anlz_dir() would have 404'd
    mapped_twoex = _asset_sibling(mapped_adp, mapped_adp.resolved.with_suffix(".2EX"))
    if mapped_twoex.resolved is None:
        raise not_found(
            "ANALYSIS_NOT_FOUND",
            f".2EX file for track {content.stable_id} is unsafe "
            f"({mapped_twoex.reason}): {content.analysis_data_path}",
        )
    twoex_path = mapped_twoex.resolved
    try:
        dat_mtime: Optional[float] = mapped_adp.resolved.stat().st_mtime
    except OSError:
        # anlz_dir() already verified the real directory exists; only a
        # mocked/synthetic path (unit tests) lands here. Skip the sidecar
        # cache write rather than fail the whole payload over it.
        dat_mtime = None
    anlz_mtime = _anlz_mtime(directory)
    cached = _load_cached_payload(content.stable_id, anlz_mtime, points)
    if cached is not None:
        if dat_mtime is not None:
            _ensure_beatgrid_issue_cached(
                content.stable_id, dat_mtime, cached["beatgrid"]["beats"]
            )
        payload = dict(cached)
        payload["cues"] = fetch_cues(content.vendor_id)
        return merge_demucs_vocals(payload, content)

    tags, unreadable_anlz = _first_tags(directory)
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
    if dat_mtime is not None:
        _ensure_beatgrid_issue_cached(content.stable_id, dat_mtime, beatgrid["beats"])
    payload: dict[str, Any] = {
        "stable_id": content.stable_id,
        "points": points,
        "waveform": {
            "kind": kind,
            "preview": _bands_payload(preview_bands, points)
            if preview_bands
            else dict(empty_bands),
            "detail": _bands_payload(detail_bands, points)
            if detail_bands
            else dict(empty_bands),
        },
        "beatgrid": beatgrid,
        "phrases": _phrases_payload(tags, times),
        # contract item 5: PVDI-derived vocal regions, three explicit states.
        "vocals": vocals_payload(twoex_path),
        # ANLZ files rekordbox wrote but pyrekordbox cannot parse. Empty for
        # a healthy track. Non-empty means some lanes below are absent because
        # their tags were unreadable -- NOT because the track has no such data.
        "unreadable_anlz": unreadable_anlz,
    }

    _store_cached_payload(content.stable_id, anlz_mtime, points, payload)
    payload["cues"] = fetch_cues(content.vendor_id)
    return merge_demucs_vocals(payload, content)


# ----- hot-cue SAVE (djmdCue Kind 1-8 write surface, edit-write-path lane) --
#
# This is the first write surface into master.plain.db: everything above
# this line opens the DB `mode=ro` / `PRAGMA query_only`. Writes stop at
# Kind 8 (slot H) on purpose -- Kind 9-11 rows are observed in the wild
# (RECON-DATA.md section 4, 24 rows) but their slot mapping is unverified,
# so this surface never guesses at it (PARITY-TODO.md "Hot-cue SAVE").
# beatgrid-editing (DEPENDENCY-PATH.md line 153) reuses _open_rw below.
#
# master.plain.db here is the STATIC decrypted working copy (CLAUDE.md /
# PARITY-TODO.md "Known data notes"), not the live rekordbox db the desktop
# app has open -- SAVE round-trips through our own read path (fetch_cues is
# always re-queried live, never cached) but does not sync back to rekordbox
# itself; that is the separate write-back-rekordbox-djay node.
#
# Quantizing to the beatgrid (DEPENDENCY-PATH "quantized position") is a
# frontend concern (beat-sync-math.quantizeToNearestBeat against the loaded
# AnlzBeatgrid) -- this surface accepts whatever in_ms it is given verbatim.


class HotCueSlotError(ValueError):
    """Unknown/unsupported hot-cue slot letter."""


def _slot_to_kind(slot: str) -> int:
    if len(slot) != 1 or slot not in HOT_CUE_SLOTS:
        raise HotCueSlotError(
            f"unsupported hot-cue slot {slot!r}; only {HOT_CUE_SLOTS} are "
            "write-supported (Kind 9-11 slot mapping unverified, see "
            "PARITY-TODO.md 'Hot-cue SAVE')"
        )
    return HOT_CUE_SLOTS.index(slot) + 1


def _msec_to_frame(msec: int) -> int:
    """Same 44.1 kHz heuristic as apps/sync/rb_writer.py (InFrame is not
    read back by fetch_cues; kept for on-disk-row authenticity only)."""
    return int(round(msec * 0.441))


def _rb_timestamp() -> str:
    now = time.time()
    struct_time = time.gmtime(now)
    millis = int((now - int(now)) * 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", struct_time) + f".{millis:03d} +00:00"


def _new_cue_id(conn: sqlite3.Connection) -> str:
    for _ in range(50):
        candidate = str(secrets.randbelow(9_000_000_000) + 1_000_000_000)
        if (
            conn.execute("SELECT 1 FROM djmdCue WHERE ID = ?", (candidate,)).fetchone()
            is None
        ):
            return candidate
    raise HTTPException(
        status_code=500,
        detail={
            "code": "CUE_ID_EXHAUSTED",
            "message": "could not allocate a unique djmdCue ID",
        },
    )


def _open_rw(path: Path, label: str) -> sqlite3.Connection:
    if not path.exists():
        raise HTTPException(
            status_code=500,
            detail={
                "code": f"{label}_UNAVAILABLE",
                "message": f"required database missing on disk: {path}",
            },
        )
    return sqlite3.connect(str(path))


_CUE_SNAPSHOT_COLUMNS = (
    "ID, Kind, InMsec, InFrame, InMpegFrame, InMpegAbs, OutMsec, OutFrame, "
    "ActiveLoop, BeatLoopSize, ColorTableIndex, Comment, updated_at"
)


def _cue_snapshot_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Serialize every destructive hot-cue field needed for an exact undo."""
    (
        cue_id,
        kind,
        in_ms,
        in_frame,
        in_mpeg_frame,
        in_mpeg_abs,
        out_ms,
        out_frame,
        active_loop,
        beat_loop_size,
        color_table_index,
        comment,
        updated_at,
    ) = row
    return {
        "id": str(cue_id),
        "kind": int(kind) if kind is not None else None,
        "in_ms": int(in_ms) if in_ms is not None else None,
        "in_frame": int(in_frame) if in_frame is not None else None,
        "in_mpeg_frame": int(in_mpeg_frame) if in_mpeg_frame is not None else None,
        "in_mpeg_abs": int(in_mpeg_abs) if in_mpeg_abs is not None else None,
        "out_ms": int(out_ms) if out_ms is not None else None,
        "out_frame": int(out_frame) if out_frame is not None else None,
        "active_loop": bool(active_loop),
        "beat_loop_size": int(beat_loop_size) if beat_loop_size is not None else None,
        "color_table_index": int(color_table_index)
        if color_table_index is not None
        else None,
        "comment": comment or None,
        "updated_at": str(updated_at) if updated_at is not None else None,
    }


def _cue_revision(
    vendor_id: str,
    kind: int,
    generation: int,
    snapshot: Optional[Mapping[str, Any]],
) -> str:
    """Opaque CAS token bound to track, slot generation, and exact state."""
    encoded = json.dumps(
        {
            "content_id": vendor_id,
            "kind": kind,
            "generation": generation,
            "snapshot": snapshot,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _live_slot_snapshot(
    conn: sqlite3.Connection,
    vendor_id: str,
    kind: int,
) -> Optional[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {_CUE_SNAPSHOT_COLUMNS} FROM djmdCue "
        "WHERE ContentID = ? AND Kind = ? AND rb_local_deleted = 0",
        (vendor_id, kind),
    ).fetchall()
    if len(rows) > 1:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "HOT_CUE_SLOT_CORRUPT",
                "message": f"slot Kind {kind} has {len(rows)} live cue rows",
            },
        )
    return _cue_snapshot_from_row(rows[0]) if rows else None


def _cue_view(slot: str, snapshot: Mapping[str, Any], revision: str) -> dict[str, Any]:
    out_ms = snapshot["out_ms"]
    is_loop = bool(out_ms is not None and out_ms > 0)
    return {
        "kind": "hot_cue",
        "slot": slot,
        "in_ms": snapshot["in_ms"],
        "out_ms": out_ms if is_loop else None,
        "is_loop": is_loop,
        "active_loop": snapshot["active_loop"],
        "beat_loop_size": snapshot["beat_loop_size"],
        "color_table_index": snapshot["color_table_index"],
        "comment": snapshot["comment"],
        "revision": revision,
    }


def fetch_hot_cue_slots(vendor_id: str) -> list[dict[str, Any]]:
    """Read all eight slot states, including revisions for empty slots."""
    master = _open_rw(MASTER_PLAIN_DB, "MASTER_DB")
    try:
        master.execute("BEGIN IMMEDIATE")
        _ensure_reversal_tables(master)
        snapshots = {
            kind: _live_slot_snapshot(master, vendor_id, kind)
            for kind in range(1, len(HOT_CUE_SLOTS) + 1)
        }
        generations = {
            kind: _slot_generation(master, vendor_id, kind)
            for kind in range(1, len(HOT_CUE_SLOTS) + 1)
        }
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()
    return [
        {
            "slot": slot,
            "cue": _cue_view(
                slot,
                snapshots[kind],
                _cue_revision(vendor_id, kind, generations[kind], snapshots[kind]),
            )
            if snapshots[kind]
            else None,
            "revision": _cue_revision(
                vendor_id, kind, generations[kind], snapshots[kind]
            ),
        }
        for kind, slot in enumerate(HOT_CUE_SLOTS, start=1)
    ]


def _require_current_revision(expected_revision: str, current_revision: str) -> None:
    if not expected_revision:
        raise HTTPException(
            status_code=428,
            detail={
                "code": "HOT_CUE_REVISION_REQUIRED",
                "message": "hot-cue mutation requires an If-Match revision",
            },
        )
    if expected_revision != current_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "HOT_CUE_REVISION_CONFLICT",
                "message": "hot-cue slot changed since it was read",
                "current_revision": current_revision,
            },
            headers={"ETag": current_revision},
        )


def _duration_ms(conn: sqlite3.Connection, vendor_id: str) -> int:
    row = conn.execute(
        "SELECT Length FROM djmdContent WHERE ID = ? AND rb_local_deleted = 0",
        (vendor_id,),
    ).fetchone()
    if row is None or row[0] is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TRACK_DURATION_UNAVAILABLE",
                "message": f"track {vendor_id} has no finite rekordbox duration",
            },
        )
    duration_s = row[0]
    if (
        isinstance(duration_s, bool)
        or not isinstance(duration_s, int)
        or duration_s < 0
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TRACK_DURATION_UNAVAILABLE",
                "message": f"track {vendor_id} has invalid rekordbox duration {duration_s!r}",
            },
        )
    return duration_s * 1000


def _validate_cue_position(
    conn: sqlite3.Connection, vendor_id: str, in_ms: int
) -> None:
    if isinstance(in_ms, bool) or not isinstance(in_ms, int):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_CUE_POSITION",
                "message": "in_ms must be a finite integer",
            },
        )
    duration_ms = _duration_ms(conn, vendor_id)
    if not 0 <= in_ms <= duration_ms:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_CUE_POSITION",
                "message": f"in_ms must satisfy 0 <= in_ms <= {duration_ms}, got {in_ms}",
            },
        )


def _restore_snapshot(
    conn: sqlite3.Connection,
    vendor_id: str,
    kind: int,
    snapshot: Mapping[str, Any],
    now: str,
) -> None:
    result = conn.execute(
        "UPDATE djmdCue SET InMsec = ?, InFrame = ?, InMpegFrame = ?, "
        "InMpegAbs = ?, OutMsec = ?, OutFrame = ?, ActiveLoop = ?, "
        "BeatLoopSize = ?, ColorTableIndex = ?, Comment = ?, "
        "rb_local_deleted = 0, updated_at = ? "
        "WHERE ID = ? AND ContentID = ? AND Kind = ?",
        (
            snapshot["in_ms"],
            snapshot["in_frame"],
            snapshot["in_mpeg_frame"],
            snapshot["in_mpeg_abs"],
            snapshot["out_ms"],
            snapshot["out_frame"],
            int(snapshot["active_loop"]),
            snapshot["beat_loop_size"],
            snapshot["color_table_index"],
            snapshot["comment"],
            now,
            snapshot["id"],
            vendor_id,
            kind,
        ),
    )
    if result.rowcount != 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "HOT_CUE_PREIMAGE_UNRESTORABLE",
                "message": "the original cue row no longer exists",
            },
        )


def _ensure_reversal_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rb_hot_cue_reversal ("
        "ID TEXT PRIMARY KEY, ContentID TEXT NOT NULL, Kind INTEGER NOT NULL, "
        "PreimageJson TEXT, PostRevision TEXT NOT NULL, CreatedAt TEXT NOT NULL, "
        "ConsumedAt TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rb_hot_cue_slot_revision ("
        "ContentID TEXT NOT NULL, Kind INTEGER NOT NULL, Generation INTEGER NOT NULL, "
        "PRIMARY KEY (ContentID, Kind))"
    )


def _slot_generation(conn: sqlite3.Connection, vendor_id: str, kind: int) -> int:
    _ensure_reversal_tables(conn)
    conn.execute(
        "INSERT OR IGNORE INTO rb_hot_cue_slot_revision (ContentID, Kind, Generation) "
        "VALUES (?, ?, 0)",
        (vendor_id, kind),
    )
    row = conn.execute(
        "SELECT Generation FROM rb_hot_cue_slot_revision WHERE ContentID = ? AND Kind = ?",
        (vendor_id, kind),
    ).fetchone()
    if row is None:
        raise RuntimeError("hot-cue slot generation was not created")
    return int(row[0])


def _bump_slot_generation(conn: sqlite3.Connection, vendor_id: str, kind: int) -> int:
    generation = _slot_generation(conn, vendor_id, kind)
    result = conn.execute(
        "UPDATE rb_hot_cue_slot_revision SET Generation = ? "
        "WHERE ContentID = ? AND Kind = ? AND Generation = ?",
        (generation + 1, vendor_id, kind, generation),
    )
    if result.rowcount != 1:
        raise RuntimeError(
            "hot-cue slot generation changed inside an exclusive transaction"
        )
    return generation + 1


def _create_reversal(
    conn: sqlite3.Connection,
    vendor_id: str,
    kind: int,
    preimage: Optional[Mapping[str, Any]],
    post_revision: str,
) -> str:
    _ensure_reversal_tables(conn)
    reversal_id = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO rb_hot_cue_reversal "
        "(ID, ContentID, Kind, PreimageJson, PostRevision, CreatedAt, ConsumedAt) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL)",
        (
            reversal_id,
            vendor_id,
            kind,
            json.dumps(preimage, sort_keys=True, separators=(",", ":"))
            if preimage
            else None,
            post_revision,
            _rb_timestamp(),
        ),
    )
    return reversal_id


def _load_reversal(
    conn: sqlite3.Connection,
    reversal_id: str,
    vendor_id: str,
    kind: int,
) -> Optional[dict[str, Any]]:
    _ensure_reversal_tables(conn)
    row = conn.execute(
        "SELECT ContentID, Kind, PreimageJson, PostRevision, ConsumedAt "
        "FROM rb_hot_cue_reversal WHERE ID = ?",
        (reversal_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "HOT_CUE_REVERSAL_NOT_FOUND",
                "message": "unknown reversal token",
            },
        )
    content_id, record_kind, preimage_json, post_revision, consumed_at = row
    if str(content_id) != vendor_id or int(record_kind) != kind:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "HOT_CUE_REVERSAL_SCOPE_CONFLICT",
                "message": "reversal token is bound to another hot-cue slot",
            },
        )
    if consumed_at is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "HOT_CUE_REVERSAL_CONSUMED",
                "message": "reversal token was already consumed",
            },
        )
    return {
        "preimage": json.loads(preimage_json) if preimage_json else None,
        "post_revision": str(post_revision),
    }


def save_hot_cue(
    vendor_id: str,
    slot: str,
    in_ms: int,
    *,
    expected_revision: str,
    comment: Optional[str] = None,
    color_table_index: Optional[int] = None,
) -> dict[str, Any]:
    """CAS-save a hot cue and return its atomic preimage for one-step undo."""
    kind = _slot_to_kind(slot)
    now = _rb_timestamp()
    master = _open_rw(MASTER_PLAIN_DB, "MASTER_DB")
    try:
        master.execute("BEGIN IMMEDIATE")
        _validate_cue_position(master, vendor_id, in_ms)
        preimage = _live_slot_snapshot(master, vendor_id, kind)
        generation = _slot_generation(master, vendor_id, kind)
        _require_current_revision(
            expected_revision,
            _cue_revision(vendor_id, kind, generation, preimage),
        )
        if preimage is not None:
            update = master.execute(
                "UPDATE djmdCue SET InMsec = ?, InFrame = ?, InMpegFrame = NULL, "
                "InMpegAbs = NULL, OutMsec = NULL, OutFrame = NULL, "
                "ActiveLoop = 0, ColorTableIndex = ?, Comment = ?, "
                "updated_at = ? WHERE ID = ? AND ContentID = ? AND Kind = ?",
                (
                    in_ms,
                    _msec_to_frame(in_ms),
                    color_table_index,
                    comment,
                    now,
                    preimage["id"],
                    vendor_id,
                    kind,
                ),
            )
            if update.rowcount != 1:
                raise RuntimeError("save_hot_cue: scoped cue row disappeared")
        else:
            cue_id = _new_cue_id(master)
            master.execute(
                "INSERT INTO djmdCue (ID, ContentID, InMsec, InFrame, "
                "InMpegFrame, InMpegAbs, OutMsec, OutFrame, Kind, "
                "ColorTableIndex, ActiveLoop, Comment, rb_local_deleted, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, 0, ?, 0, "
                "?, ?)",
                (
                    cue_id,
                    vendor_id,
                    in_ms,
                    _msec_to_frame(in_ms),
                    kind,
                    color_table_index,
                    comment,
                    now,
                    now,
                ),
            )
        current = _live_slot_snapshot(master, vendor_id, kind)
        if current is None:
            raise RuntimeError("save_hot_cue: committed slot disappeared")
        generation = _bump_slot_generation(master, vendor_id, kind)
        revision = _cue_revision(vendor_id, kind, generation, current)
        reversal_id = _create_reversal(
            master,
            vendor_id,
            kind,
            preimage,
            revision,
        )
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()
    return {
        "cue": _cue_view(slot, current, revision),
        "reversal": {"reversal_id": reversal_id},
    }


def clear_hot_cue(
    vendor_id: str,
    slot: str,
    *,
    expected_revision: str,
) -> dict[str, Any]:
    """CAS-clear a hot cue and return its atomic preimage for restore."""
    kind = _slot_to_kind(slot)
    master = _open_rw(MASTER_PLAIN_DB, "MASTER_DB")
    try:
        master.execute("BEGIN IMMEDIATE")
        preimage = _live_slot_snapshot(master, vendor_id, kind)
        generation = _slot_generation(master, vendor_id, kind)
        _require_current_revision(
            expected_revision,
            _cue_revision(vendor_id, kind, generation, preimage),
        )
        if preimage is not None:
            update = master.execute(
                "UPDATE djmdCue SET rb_local_deleted = 1, updated_at = ? "
                "WHERE ID = ? AND ContentID = ? AND Kind = ?",
                (_rb_timestamp(), preimage["id"], vendor_id, kind),
            )
            if update.rowcount != 1:
                raise RuntimeError("clear_hot_cue: scoped cue row disappeared")
        current = _live_slot_snapshot(master, vendor_id, kind)
        generation = _bump_slot_generation(master, vendor_id, kind)
        revision = _cue_revision(vendor_id, kind, generation, current)
        reversal_id = _create_reversal(
            master,
            vendor_id,
            kind,
            preimage,
            revision,
        )
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()
    return {
        "cue": None,
        "revision": revision,
        "reversal": {"reversal_id": reversal_id},
    }


def restore_hot_cue(
    vendor_id: str,
    slot: str,
    *,
    expected_revision: str,
    reversal_id: str,
) -> dict[str, Any]:
    """CAS-restore one server-authoritative, single-use reversal token."""
    kind = _slot_to_kind(slot)
    master = _open_rw(MASTER_PLAIN_DB, "MASTER_DB")
    try:
        master.execute("BEGIN IMMEDIATE")
        reversal = _load_reversal(master, reversal_id, vendor_id, kind)
        current = _live_slot_snapshot(master, vendor_id, kind)
        generation = _slot_generation(master, vendor_id, kind)
        revision = _cue_revision(vendor_id, kind, generation, current)
        _require_current_revision(expected_revision, revision)
        if reversal["post_revision"] != revision:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "HOT_CUE_REVERSAL_STALE",
                    "message": "hot-cue slot changed after the reversible mutation",
                    "current_revision": revision,
                },
            )
        preimage = reversal["preimage"]
        if preimage is None:
            if current is not None:
                master.execute(
                    "UPDATE djmdCue SET rb_local_deleted = 1, updated_at = ? "
                    "WHERE ID = ? AND ContentID = ? AND Kind = ?",
                    (_rb_timestamp(), current["id"], vendor_id, kind),
                )
        else:
            _restore_snapshot(master, vendor_id, kind, preimage, _rb_timestamp())
        master.execute(
            "UPDATE rb_hot_cue_reversal SET ConsumedAt = ? "
            "WHERE ID = ? AND ContentID = ? AND Kind = ? AND ConsumedAt IS NULL",
            (_rb_timestamp(), reversal_id, vendor_id, kind),
        )
        restored = _live_slot_snapshot(master, vendor_id, kind)
        generation = _bump_slot_generation(master, vendor_id, kind)
        revision = _cue_revision(vendor_id, kind, generation, restored)
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()
    return {
        "cue": _cue_view(slot, restored, revision) if restored else None,
        "revision": revision,
    }


__all__ = [
    "ANLZ_CACHE_DIR",
    "ANLZ_CACHE_SCHEMA",
    "ARTWORK_FILENAMES",
    "AUDIO_MEDIA_TYPES",
    "BEATGRID_ISSUE_CACHE_DIR",
    "BEATGRID_ISSUE_CACHE_SCHEMA",
    "FILE_EXISTS_TTL_S",
    "HOT_CUE_SLOTS",
    "HotCueSlotError",
    "MASTER_PLAIN_DB",
    "PREVIEW_COLUMNS",
    "RbContent",
    "RbRowMeta",
    "STREAMING_PREFIXES",
    "VOCAL_CACHE_DIR",
    "VOCAL_INTENSITY_MIN",
    "VOCAL_MERGE_GAP_S",
    "VOCAL_MIN_REGION_S",
    "anlz_dir",
    "artwork_file",
    "audio_file",
    "resolve_playable_audio",
    "build_anlz_payload",
    "build_track_rows",
    "bulk_availability",
    "bulk_file_exists",
    "bulk_rb_meta",
    "cached_beatgrid_issue",
    "clear_hot_cue",
    "count_cues",
    "demucs_vocals_payload",
    "fetch_cues",
    "fetch_hot_cue_slots",
    "is_streaming_path",
    "merge_demucs_vocals",
    "not_found",
    "preview_strip",
    "read_pvdi",
    "resolve_content",
    "resolve_share_path",
    "restore_hot_cue",
    "save_hot_cue",
    "vocals_payload",
]
