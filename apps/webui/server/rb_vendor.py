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
from apps.webui.server.rb_vendor_pkg import cues as _hot_cue_model
from apps.webui.server.rb_vendor_pkg import writer as _hot_cue_writer
from apps.webui.server.rb_vendor_pkg.cues import HotCueSlotError as HotCueSlotError
from apps.webui.server.rb_vendor_pkg.reversal import (
    _ensure_reversal_tables as _ensure_reversal_tables,
)
from apps.webui.server.rb_vendor_pkg.writer import _open_rw as _open_rw

# The djmdCue row -> snapshot mapping is shared by the write surface (C10,
# cues.py) and the cue read (C4, rb_vendor_pkg/db.py). cues.py is its one
# home; db.py currently reaches it back through this facade by design (see
# its module docstring), so the name has to stay bound here.
from apps.webui.server.rb_vendor_pkg.cues import _cue_snapshot_from_row

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
# Canonical home is rb_vendor_pkg.cues (cue-domain vocabulary, not a path
# constant); re-exported here so `rb_vendor.HOT_CUE_SLOTS` keeps working.
HOT_CUE_SLOTS: str = _hot_cue_model.HOT_CUE_SLOTS
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

# ----- hot-cue write surface (moved to rb_vendor_pkg, T3b slice S4) --------
#
# The implementation now lives in apps/webui/server/rb_vendor_pkg/
# ({cues,reversal,writer}.py) per .planning/t3b-decomposition-map.md section 2
# rows 10-12. What stays here is the wiring: the package takes its
# master.plain.db connection by injection rather than owning a path constant,
# and these wrappers are what bind it to THIS module's MASTER_PLAIN_DB /
# _open_rw / _open_ro. Both factories resolve those names from this module's
# namespace at call time, so the long-standing test seams
# (monkeypatch.setattr(rb_vendor, "MASTER_PLAIN_DB", ...) and
# monkeypatch.setattr(rb_vendor, "_open_rw", ...)) still bite.
#


def _connect_master_rw() -> sqlite3.Connection:
    return _open_rw(MASTER_PLAIN_DB, "MASTER_DB")


def fetch_hot_cue_slots(vendor_id: str) -> list[dict[str, Any]]:
    """Read all eight slot states, including revisions for empty slots."""
    return _hot_cue_writer.fetch_hot_cue_slots(vendor_id, open_rw=_connect_master_rw)


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
    return _hot_cue_writer.save_hot_cue(
        vendor_id,
        slot,
        in_ms,
        expected_revision=expected_revision,
        comment=comment,
        color_table_index=color_table_index,
        open_rw=_connect_master_rw,
    )


def clear_hot_cue(
    vendor_id: str,
    slot: str,
    *,
    expected_revision: str,
) -> dict[str, Any]:
    """CAS-clear a hot cue and return its atomic preimage for restore."""
    return _hot_cue_writer.clear_hot_cue(
        vendor_id,
        slot,
        expected_revision=expected_revision,
        open_rw=_connect_master_rw,
    )


def restore_hot_cue(
    vendor_id: str,
    slot: str,
    *,
    expected_revision: str,
    reversal_id: str,
) -> dict[str, Any]:
    """CAS-restore one server-authoritative, single-use reversal token."""
    return _hot_cue_writer.restore_hot_cue(
        vendor_id,
        slot,
        expected_revision=expected_revision,
        reversal_id=reversal_id,
        open_rw=_connect_master_rw,
    )


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
