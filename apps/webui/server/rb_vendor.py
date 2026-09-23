"""Compatibility facade over the rekordbox adapter and its staged siblings.

**There is no implementation left in this file.** Every name below is
re-exported from the module that owns it, per
``.planning/t3b-decomposition-map.md`` section 2. The file survives only so
the 10 route importers and the test modules that reach these names through
``rb_vendor`` keep working; the map deletes it once they are repointed.

Where the implementation went:

    apps/adapters/rekordbox/config.py   path constants + cache schemas (C0)
    apps/adapters/rekordbox/models.py   RbContent, RbRowMeta (C0)
    apps/adapters/rekordbox/errors.py   not_found, _open_ro (C1)
    apps/adapters/rekordbox/paths.py    content + asset resolution (C1)
    apps/adapters/rekordbox/cues.py     hot-cue modelling (C10)
    apps/adapters/rekordbox/reversal.py reversal tokens (C10)
    apps/adapters/rekordbox/writer.py   the only RW surface (C10)
    rb_vendor_pkg/anlz.py               PMAI/PVDI/tag decode + payload (C2/3/6/9)
    rb_vendor_pkg/db.py                 read-only master.plain.db (C4)
    rb_vendor_pkg/track_rows.py         browser read model (C5)
    rb_vendor_pkg/anlz_cache.py         anlz JSON cache (C7)
    rb_vendor_pkg/beatgrid_issue_cache.py  beatgrid-issue sidecar (C8)
    (moved out) analysis_waveform/native.py  optional Rust waveform backend gate
                                        (ported from main, not a T3b cluster)

The five ``rb_vendor_pkg`` modules are still under ``apps.webui`` because
each imports a module that has not left it yet: ``beatgrid_diagnostics``,
``etag`` and ``stem_artifacts``. All three are pure (stdlib, pydantic and
``apps.shared`` only), so the move is cheap -- but it is the ``.importlinter``
DEBT block's inversion, not T3b's, and doing it here would import
``apps.webui`` from ``apps.adapters``, which that file's hard-fail contract
forbids.

**Config constants are NOT re-exported.** ``MASTER_PLAIN_DB`` and friends are
rebindable overrides: a test or ``.planning/e2e-gating/run_daemon.py`` assigns
to them to point the adapter at a different library. Re-exporting them here
would create a second binding that assignment does not reach, so half the
readers would follow the override and half would not. Assign to
``apps.adapters.rekordbox.config`` instead; see that module's docstring.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from apps.adapters.rekordbox import config as _config
from apps.adapters.rekordbox import cues as _hot_cue_model
from apps.adapters.rekordbox import writer as _hot_cue_writer
from apps.adapters.rekordbox.cues import HotCueSlotError, _cue_snapshot_from_row
from apps.adapters.rekordbox.errors import _open_ro, not_found
from apps.adapters.rekordbox.models import RbContent, RbRowMeta
from apps.adapters.rekordbox.paths import (
    _asset_sibling,
    anlz_dir,
    artwork_file,
    audio_file,
    empty_anlz_payload,
    empty_hot_cue_slots,
    is_streaming_path,
    local_artwork,
    local_artwork_available,
    local_audio_file,
    local_track_file_tags,
    local_track_row,
    resolve_asset_path,
    resolve_content,
    resolve_playable_audio,
    resolve_share_path,
)
from apps.adapters.rekordbox.writer import _open_rw
from apps.analysis_waveform.bands import (
    _bands_payload,
    _bands_payload_python,
    _downsample_max,
    _mono_bands,
    _tri_bands,
)
from apps.analysis_waveform.local_waveform import (
    LocalDecodeUnavailable,
    ensure_local_peaks,
    local_anlz_payload,
    local_preview_strip,
)
from apps.analysis_waveform.native import (
    _WAVEFORM_BACKEND_REQUEST,
    _WAVEFORM_NATIVE,
    _WAVEFORM_NATIVE_IMPORT_ERROR,
    waveform_materialization_backend,
    waveform_materialization_backend_request,
    waveform_materialization_status,
)
from apps.shared.platform_paths import resolve_library_path

# ANLZ decode, caches, cue reads and the browser read model (C2-C9).
from apps.webui.server.rb_vendor_pkg.anlz import (
    _PREVIEW_SOURCES,
    _PVDI_FIXED_HEADER,
    _PVDI_HOP,
    _PVDI_RATE,
    _PWAV_HEIGHT_MASK,
    _PWV4_LUMINANCE_BYTE,
    PREVIEW_COLUMNS,
    VOCAL_INTENSITY_MIN,
    VOCAL_MERGE_GAP_S,
    VOCAL_MIN_REGION_S,
    _beatgrid_payload,
    _first_tags,
    _iter_pmai_sections,
    _peak_downsample_cols,
    _phrases_payload,
    _read_pwav_mono,
    _read_pwv4_mono,
    _read_pwv6_tri,
    _vocal_regions,
    build_anlz_payload,
    demucs_vocals_payload,
    merge_demucs_vocals,
    preview_strip,
    read_pvdi,
    vocals_for_content,
    vocals_payload,
)
from apps.webui.server.rb_vendor_pkg.anlz_cache import (
    _anlz_mtime,
    _cache_lock,
    _cache_path,
    _load_cached_payload,
    _store_cached_payload,
)
from apps.webui.server.rb_vendor_pkg.beatgrid_issue_cache import (
    _beatgrid_issue_cache_path,
    _ensure_beatgrid_issue_cached,
    _read_beatgrid_issue_cache_entry,
    _store_beatgrid_issue_cache,
    cached_beatgrid_issue,
)
from apps.webui.server.rb_vendor_pkg.db import count_cues, fetch_cues, playlist_order_index
from apps.webui.server.rb_vendor_pkg.row_hydration_cache import (
    _PREVIEW_CACHE,
    _VOCALS_CACHE,
)
from apps.webui.server.rb_vendor_pkg.track_rows import (
    AvailabilityProbeMode,
    FileAvailabilityStatus,
    build_track_rows,
    bulk_availability,
    bulk_availability_for_playlist_summary,
    bulk_availability_status,
    bulk_file_exists,
    bulk_file_size,
    bulk_probe_paths,
    bulk_quality,
    bulk_rb_meta,
)

log = logging.getLogger(__name__)

# Cue-domain vocabulary, owned by adapters/rekordbox/cues.py; bound here so
# ``rb_vendor.HOT_CUE_SLOTS`` keeps working.
HOT_CUE_SLOTS: str = _hot_cue_model.HOT_CUE_SLOTS


# ----- hot-cue write surface: connection wiring ------------------------------
#
# The adapter takes its master.plain.db connection by injection rather than
# owning a path constant (see adapters/rekordbox/writer.py). These wrappers
# are that injection, and they read config.MASTER_PLAIN_DB at call time so the
# override seam stays live.


def _connect_master_rw() -> sqlite3.Connection:
    return _open_rw(_config.MASTER_PLAIN_DB, "MASTER_DB")


def _connect_master_ro() -> sqlite3.Connection:
    return _open_ro(_config.MASTER_PLAIN_DB, "MASTER_DB")


def fetch_hot_cue_slots(vendor_id: str) -> list[dict[str, Any]]:
    """Read all eight slot states, including revisions for empty slots."""
    return _hot_cue_writer.fetch_hot_cue_slots(vendor_id, open_ro=_connect_master_ro)


def save_hot_cue(
    vendor_id: str,
    slot: str,
    in_ms: int,
    *,
    expected_revision: str,
    comment: str | None = None,
    color_table_index: int | None = None,
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


# Every name this module re-exports, private helpers included. That is
# unusual for an __all__ and correct for this one: the file's entire job is
# re-export, and a name absent here is a name no importer may rely on. It is
# also what marks the imports above as deliberate rather than unused, now
# that ruff lints this file.
__all__ = [
    "HOT_CUE_SLOTS",
    "PREVIEW_COLUMNS",
    "VOCAL_INTENSITY_MIN",
    "VOCAL_MERGE_GAP_S",
    "VOCAL_MIN_REGION_S",
    "_PREVIEW_CACHE",
    "_PREVIEW_SOURCES",
    "_PVDI_FIXED_HEADER",
    "_PVDI_HOP",
    "_PVDI_RATE",
    "_PWAV_HEIGHT_MASK",
    "_PWV4_LUMINANCE_BYTE",
    "_VOCALS_CACHE",
    "_WAVEFORM_BACKEND_REQUEST",
    "_WAVEFORM_NATIVE",
    "_WAVEFORM_NATIVE_IMPORT_ERROR",
    "AvailabilityProbeMode",
    "FileAvailabilityStatus",
    "HotCueSlotError",
    "LocalDecodeUnavailable",
    "RbContent",
    "RbRowMeta",
    "_anlz_mtime",
    "_asset_sibling",
    "_bands_payload",
    "_bands_payload_python",
    "_beatgrid_issue_cache_path",
    "_beatgrid_payload",
    "_cache_lock",
    "_cache_path",
    "_cue_snapshot_from_row",
    "_downsample_max",
    "_ensure_beatgrid_issue_cached",
    "_first_tags",
    "_iter_pmai_sections",
    "_load_cached_payload",
    "_mono_bands",
    "_open_ro",
    "_open_rw",
    "_peak_downsample_cols",
    "_phrases_payload",
    "_read_beatgrid_issue_cache_entry",
    "_read_pwav_mono",
    "_read_pwv4_mono",
    "_read_pwv6_tri",
    "_store_beatgrid_issue_cache",
    "_store_cached_payload",
    "_tri_bands",
    "_vocal_regions",
    "anlz_dir",
    "artwork_file",
    "audio_file",
    "build_anlz_payload",
    "build_track_rows",
    "bulk_availability",
    "bulk_availability_for_playlist_summary",
    "bulk_availability_status",
    "bulk_file_exists",
    "bulk_file_size",
    "bulk_probe_paths",
    "bulk_quality",
    "bulk_rb_meta",
    "cached_beatgrid_issue",
    "clear_hot_cue",
    "count_cues",
    "demucs_vocals_payload",
    "empty_anlz_payload",
    "empty_hot_cue_slots",
    "ensure_local_peaks",
    "fetch_cues",
    "fetch_hot_cue_slots",
    "is_streaming_path",
    "local_anlz_payload",
    "local_artwork",
    "local_artwork_available",
    "local_audio_file",
    "local_preview_strip",
    "local_track_file_tags",
    "local_track_row",
    "merge_demucs_vocals",
    "not_found",
    "playlist_order_index",
    "preview_strip",
    "read_pvdi",
    "resolve_asset_path",
    "resolve_content",
    "resolve_library_path",
    "resolve_playable_audio",
    "resolve_share_path",
    "restore_hot_cue",
    "save_hot_cue",
    "vocals_for_content",
    "vocals_payload",
    "waveform_materialization_backend",
    "waveform_materialization_backend_request",
    "waveform_materialization_status",
]
