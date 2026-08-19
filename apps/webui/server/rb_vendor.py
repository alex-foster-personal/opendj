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

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from fastapi import HTTPException

from apps.shared import fs_residency, platform_paths
from apps.shared.paths import DATA_DIR as _PATHS_DATA_DIR
from apps.shared.platform_paths import (
    MappedPath,
)
from apps.shared.platform_paths import (
    resolve_library_path as resolve_library_path,
)
from apps.shared.state import locations as track_locations
from apps.vocals import cache as vocal_cache

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

# T3b S2: the anlz-cache write lock (formerly here as _ANLZ_CACHE_LOCKS_GUARD
# / _ANLZ_CACHE_LOCKS) moved with _cache_lock into rb_vendor_pkg/anlz_cache.py
# and rb_vendor_pkg/beatgrid_issue_cache.py (each now owns its own private
# lock dict; see anlz_cache.py's module docstring for why splitting one
# shared dict into two is behavior-preserving).


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


# ----- ANLZ: PMAI waveform decode + PVDI vocals -------------------------------
# Moved to rb_vendor_pkg/anlz.py (T3b slice S1, .planning/t3b-decomposition-map.md
# clusters C2 + C3). Re-imported here so every existing caller
# (``rb_vendor.preview_strip(...)``, ``rb_vendor.vocals_payload(...)``, test
# monkeypatches, etc.) keeps working unchanged -- see anlz.py's module
# docstring for why the cross-references back into this file are local
# imports rather than a module-level circular import.
from apps.webui.server.rb_vendor_pkg.anlz import (
    PREVIEW_COLUMNS,
    VOCAL_INTENSITY_MIN,
    VOCAL_MERGE_GAP_S,
    VOCAL_MIN_REGION_S,
    _PREVIEW_CACHE,
    _PREVIEW_LOCK,
    _PREVIEW_SOURCES,
    _PVDI_FIXED_HEADER,
    _PVDI_HOP,
    _PVDI_RATE,
    _PWAV_HEIGHT_MASK,
    _PWV4_LUMINANCE_BYTE,
    _iter_pmai_sections,
    _peak_downsample_cols,
    _read_pwav_mono,
    _read_pwv4_mono,
    _read_pwv6_tri,
    _vocal_regions,
    demucs_vocals_payload,
    merge_demucs_vocals,
    preview_strip,
    read_pvdi,
    vocals_for_content,
    vocals_payload,
)

# T3b S2: moved to rb_vendor_pkg/db.py (.planning/t3b-decomposition-map.md
# target #9, adapters/rekordbox/db.py). Re-exported here so the 10 route
# importers and the test suite (tests/webui/test_rb_vendor_cache.py,
# conftest.py's _stub_rb_vendor monkeypatch of playlist_order_index) see no
# behavior change.
from apps.webui.server.rb_vendor_pkg.db import count_cues, fetch_cues
from apps.webui.server.rb_vendor_pkg.db import (
    playlist_order_index as playlist_order_index,
)

# ----- bulk row hydration (contract items 1-4) --------------------------------
# Moved to rb_vendor_pkg/track_rows.py (T3b S3, .planning/t3b-decomposition-map.md
# item 15); re-exported here (explicit self-aliased re-export, matching the
# ``resolve_library_path as resolve_library_path`` precedent above) so the
# route importers and test modules that reach these through ``rb_vendor``
# never change until the facade is retired in T3b wave 4 (S8).
from .rb_vendor_pkg.track_rows import build_track_rows as build_track_rows
from .rb_vendor_pkg.track_rows import bulk_availability as bulk_availability
from .rb_vendor_pkg.track_rows import bulk_file_exists as bulk_file_exists
from .rb_vendor_pkg.track_rows import bulk_file_size as bulk_file_size
from .rb_vendor_pkg.track_rows import bulk_quality as bulk_quality
from .rb_vendor_pkg.track_rows import bulk_rb_meta as bulk_rb_meta

# ----- ANLZ payload (waveform + beatgrid + phrases) ---------------------------
# Tag decode (_downsample_max.._phrases_payload) moved to rb_vendor_pkg/anlz.py
# (T3b slice S1, cluster C6); re-imported below. The file/beatgrid-issue caches
# right below (_anlz_mtime onward, cluster C7/C8) have NOT moved.
from apps.webui.server.rb_vendor_pkg.anlz import (
    _bands_payload,
    _beatgrid_payload,
    _downsample_max,
    _first_tags,
    _mono_bands,
    _phrases_payload,
    _tri_bands,
)


# ----- ANLZ JSON file cache + beatgrid-issue sidecar cache ------------------
# T3b S2: moved to rb_vendor_pkg/anlz_cache.py and
# rb_vendor_pkg/beatgrid_issue_cache.py (.planning/t3b-decomposition-map.md
# targets #13 and #14, store/caches/anlz_cache.py and
# store/caches/beatgrid_issue_cache.py). Re-exported here so
# build_anlz_payload below (still C9, S1's slice) and the pinning test suite
# (tests/webui/test_rb_vendor_units.py interrupted-write / concurrent-writer
# / concurrent-reader cases, tests/webui/test_rb_vendor_cache.py) see no
# behavior change.
from apps.webui.server.rb_vendor_pkg.anlz_cache import (
    _anlz_mtime as _anlz_mtime,
)
from apps.webui.server.rb_vendor_pkg.anlz_cache import (
    _cache_lock as _cache_lock,
)
from apps.webui.server.rb_vendor_pkg.anlz_cache import (
    _cache_path as _cache_path,
)
from apps.webui.server.rb_vendor_pkg.anlz_cache import (
    _load_cached_payload as _load_cached_payload,
)
from apps.webui.server.rb_vendor_pkg.anlz_cache import (
    _store_cached_payload as _store_cached_payload,
)

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
from apps.webui.server.rb_vendor_pkg.beatgrid_issue_cache import (
    _beatgrid_issue_cache_path as _beatgrid_issue_cache_path,
)
from apps.webui.server.rb_vendor_pkg.beatgrid_issue_cache import (
    _ensure_beatgrid_issue_cached as _ensure_beatgrid_issue_cached,
)
from apps.webui.server.rb_vendor_pkg.beatgrid_issue_cache import (
    _read_beatgrid_issue_cache_entry as _read_beatgrid_issue_cache_entry,
)
from apps.webui.server.rb_vendor_pkg.beatgrid_issue_cache import (
    _store_beatgrid_issue_cache as _store_beatgrid_issue_cache,
)
from apps.webui.server.rb_vendor_pkg.beatgrid_issue_cache import cached_beatgrid_issue


# ----- ANLZ payload orchestrator -----------------------------------------------
# build_anlz_payload moved to rb_vendor_pkg/anlz.py (T3b slice S1, cluster C9);
# re-imported below.
from apps.webui.server.rb_vendor_pkg.anlz import build_anlz_payload

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
