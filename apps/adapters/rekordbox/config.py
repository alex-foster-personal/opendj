"""Path constants, cache schemas, and the in-process file-existence cache.

Moved verbatim from ``apps/webui/server/rb_vendor.py`` C0 (source lines
75-169 on ``af--t4-design``) per ``.planning/t3b-decomposition-map.md``
section 2 target #2.

**Every constant here is a rebindable module attribute, on purpose.**
``.planning/e2e-gating/run_daemon.py`` and the test suite point the adapter
at a different library by assigning to these names on the module object.
That only works while every reader dereferences ``config.<NAME>`` at call
time; a ``from .config import MASTER_PLAIN_DB`` in a consumer captures the
value at import and silently stops honoring the override. The map calls this
out at target #2 and it is the single sharpest constraint on this file.

Those overrides used to be written on the ``rb_vendor`` module, which is why
T3b wave 4 had to repoint them: with the constants read from here, patching
``rb_vendor`` would leave half the readers on the real library and half on
the fixture, which is worse than either.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from apps.shared import platform_paths
from apps.shared.paths import DATA_DIR as _PATHS_DATA_DIR
from apps.vocals import cache as vocal_cache

# MDT_DATA_DIR: explicit override so a backend run against an unpacked
# scripts/data_snapshot.py pack (e.g. on a Windows box, or any Mac dev dir
# other than the default) never touches apps.shared.paths' hardcoded layout.
# Read once at import; every constant below is rederived from the SAME
# DATA_DIR so there is one source of truth. Constant NAMES are unchanged.
_MDT_DATA_DIR_ENV: str | None = os.environ.get("MDT_DATA_DIR")
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
# Peaks this repo decoded ITSELF (ffmpeg), for tracks with no rekordbox ANLZ
# to read. Same on-disk contract as ANLZ_CACHE_DIR -- schema-versioned,
# source-revalidated, atomically published -- but split in two per track:
# ``{stable_id}.json`` holds the full detail envelope (tens of KB) that only
# the single-track /anlz fetch wants, and ``{stable_id}.strip.json`` holds the
# 120-column browser strip (a few hundred bytes) that the library row
# hydration reads once per visible row. Same reasoning as
# BEATGRID_ISSUE_CACHE_DIR above: a per-row read must not drag a waveform
# payload through JSON.
LOCAL_WAVEFORM_CACHE_DIR: Path = DATA_DIR / "state" / "local-waveform-cache"
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
# Re-export, not a copy: the one prefix set lives in platform_paths (T3b D1).
STREAMING_PREFIXES: tuple[str, ...] = platform_paths.STREAMING_PREFIXES
ARTWORK_FILENAMES: dict[str, str] = {
    "s": "artwork_s.jpg",
    "m": "artwork_m.jpg",
    "orig": "artwork.jpg",
}

# Cached anlz JSON schema version. Bump whenever the payload shape or any
# decode semantics change (band order fix, vocals field, ...) so stale
# cache entries self-heal by recomputing instead of serving old shapes.
# 3 adds the required `beatgrid.source` discriminator ("own" | "rekordbox",
# NATIVE-01). A schema-2 entry has no `source` key at all, and a consumer that
# fails closed on an unknown source would render every cached rekordbox track's
# grid controls inert until the file happened to be re-decoded.
ANLZ_CACHE_SCHEMA: int = 3
# Bump whenever beatgrid_diagnostics' output shape or thresholds change.
BEATGRID_ISSUE_CACHE_SCHEMA: int = 1
# Bump whenever the local waveform cache ENTRY SHAPE changes, so stale entries
# recompute instead of being rendered against a different convention. 2 is the
# NATIVE-06 tri-band entry (``peaks_b64`` holds (n, 3) columns, not (n,)).
# What the peaks MEAN - rate, column density, crossovers, filter order - is
# gated separately and automatically by
# ``apps.analysis_waveform.local_waveform.PEAKS_VERSION``, which is derived
# from those constants rather than hand-maintained here.
LOCAL_WAVEFORM_CACHE_SCHEMA: int = 2

# --- file-existence cache ---
# file_exists is disk truth (FR-1 item 4): playable local bytes, not merely
# an inode. iCloud dataless stubs (st_size > 0, st_blocks == 0) count as
# missing -- see apps.shared.fs_residency. Per-path results are cached for
# FILE_EXISTS_TTL_S so a listing request never stats 8k files -- one bulk
# stat pass warms the cache, then repeats are dict lookups until the TTL
# lapses (30 s keeps "file restored by reconcile" visible quickly).
# The cache holds materialised st_size (None = missing or stub), so the
# SAME pass that answers file_exists also feeds audio_quality.classify.
from apps.shared.runtime_policy import FILE_EXISTS_TTL_S  # noqa: E402

_FILE_EXISTS_LOCK = threading.Lock()
_FILE_EXISTS_CACHE: dict[str, tuple[float, int | None]] = {}


__all__ = [
    "ANLZ_CACHE_DIR",
    "ANLZ_CACHE_SCHEMA",
    "ARTWORK_FILENAMES",
    "AUDIO_MEDIA_TYPES",
    "BEATGRID_ISSUE_CACHE_DIR",
    "BEATGRID_ISSUE_CACHE_SCHEMA",
    "DATA_DIR",
    "FILE_EXISTS_TTL_S",
    "LOCAL_WAVEFORM_CACHE_DIR",
    "LOCAL_WAVEFORM_CACHE_SCHEMA",
    "MASTER_PLAIN_DB",
    "STATE_DB",
    "STREAMING_PREFIXES",
    "VOCAL_CACHE_DIR",
]
