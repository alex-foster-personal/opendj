"""stable_id -> rekordbox content, and content -> on-disk asset.

Moved verbatim from ``apps/webui/server/rb_vendor.py`` C1 (source lines
226-563 on ``af--t4-design``) per ``.planning/t3b-decomposition-map.md``
section 2 target #4. The resolution chain is unchanged:

    state.db ``tracks`` -> ``track_vendor_ids`` (vendor='rekordbox')
    -> vendor_id -> ``djmdContent`` row in ``data/master.plain.db``
    (decrypted copy, opened read-only) -> FolderPath / ImagePath /
    AnalysisDataPath resolved on disk via the share-root rule.

Fail-fast: every unresolved step raises with an explicit ``{"code",
"message"}`` detail; there is no silent None anywhere. The symlink-escape
guards live in :mod:`apps.shared.platform_paths` and are pinned by
``tests/webui/test_rb_vendor_portability.py``, which this move keeps green.

Config constants are read as ``config.<NAME>`` inside function bodies rather
than imported by value, because they are rebindable overrides -- see
:mod:`.config`.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from apps.shared import fs_residency, platform_paths
from apps.shared._mutagen import HAS_MUTAGEN
from apps.shared.platform_paths import MappedPath
from apps.shared.state import locations as track_locations

from . import config
from .cues import HOT_CUE_SLOTS
from .errors import _open_ro, not_found, unavailable
from .models import RbContent


def resolve_content(stable_id: str) -> RbContent:
    """Resolve a stable_id to its rekordbox content row, failing explicitly."""
    state = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        track_row = state.execute(
            "SELECT 1 FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()
        if track_row is None:
            raise not_found("TRACK_NOT_FOUND", f"unknown stable_id {stable_id}")
        vendor_row = state.execute(
            "SELECT vendor_id FROM track_vendor_ids "
            "WHERE stable_id = ? AND vendor = 'rekordbox' AND deleted_at IS NULL",
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

    master = _open_ro(config.MASTER_PLAIN_DB, "MASTER_DB")
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
            f"djmdContent row in {config.MASTER_PLAIN_DB.name}",
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


def is_streaming_path(folder_path: str | None) -> bool:
    """True iff ``folder_path`` is a non-empty streaming-service URI.

    Alias of :func:`apps.shared.platform_paths.is_streaming_uri`, which is
    the one definition (T3b map D1). Empty/None stays False here: this feeds
    the wire field ``is_streaming`` in ``build_track_rows``, and a track
    with no FolderPath is pathless, not streaming. Callers asking "is there
    a local file" want ``platform_paths.is_unplayable_path`` instead.
    """
    return platform_paths.is_streaming_uri(folder_path)


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
    media_type = config.AUDIO_MEDIA_TYPES.get(path.suffix.lower())
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
    state = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        row = state.execute(
            "SELECT file_path FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
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
    media_type = config.AUDIO_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "AUDIO_FORMAT_UNSUPPORTED",
                "message": f"unsupported audio extension {path.suffix!r}: {path}",
            },
        )
    return path, media_type


def _resolve_local_audio_path(file_path: str | None) -> Path | None:
    """Resolved, materialised on-disk path for a state-layer ``file_path``.

    ``None`` when the path is absent, unresolvable on this platform, or not
    materialised (dataless/iCloud stub) -- same residency gate as
    :func:`local_audio_file`. Shared by :func:`local_artwork` and
    :func:`local_artwork_available` so both agree on what "the file exists"
    means, and both check it BEFORE asking whether a reader exists.
    """
    if not file_path:
        return None
    mapped = resolve_asset_path(file_path)
    if mapped.resolved is None or not fs_residency.is_materialised(mapped.resolved):
        return None
    return mapped.resolved


def local_artwork(stable_id: str) -> tuple[bytes, str]:
    """Embedded-tag cover art for a track with NO rekordbox vendor mapping.

    :func:`artwork_file` needs a ``djmdContent.ImagePath``, which a locally
    imported file never has, so ``/artwork`` 404'd for every such row
    (PARITY-TODO). This reads the same on-disk file :func:`local_audio_file`
    resolves and returns the real embedded picture frame instead -- never a
    generated or placeholder image, and never resized (there is no
    pre-rendered s/m variant for embedded art, unlike the rekordbox path).

    ``mutagen`` is an opt-in ``[tags]`` extra (GPL vs this wheel's Apache
    license, see ``apps.shared._mutagen``), so a build that omits it cannot
    tell a track with no embedded picture apart from one it never checked.
    Collapsing that into ``ARTWORK_NOT_FOUND`` would be a guessed verdict, so
    a track with a real, resolvable file but no reader raises 503
    ``ARTWORK_READER_UNAVAILABLE`` instead -- loud and distinct from "checked,
    no art". Residency is checked FIRST: a stale path, a missing file, or a
    streaming URI has no file to read regardless of whether a reader exists,
    so those still 404 ``ARTWORK_NOT_FOUND`` even when mutagen is absent.
    """
    file_path, _duration_ms = local_track_row(stable_id)
    resolved = _resolve_local_audio_path(file_path)
    if resolved is None:
        raise not_found(
            "ARTWORK_NOT_FOUND",
            f"track {stable_id} has no resolvable local file "
            f"(file_path={file_path!r})",
        )
    if not HAS_MUTAGEN:
        raise unavailable(
            "ARTWORK_READER_UNAVAILABLE",
            f"track {stable_id} has a resolvable local file but the optional "
            "'mutagen' tag reader is not installed (pip install music-dj-tools[tags])",
        )
    from apps.shared import audio_files as _audio_files

    embedded = _audio_files.read_embedded_artwork(resolved)
    if embedded is None:
        raise not_found(
            "ARTWORK_NOT_FOUND",
            f"track {stable_id} has a local file with no embedded artwork tag "
            f"(file_path={file_path!r})",
        )
    return embedded


def local_artwork_available(file_path: str | None) -> bool | None:
    """Tri-state local-track counterpart of ``artwork_available`` for a
    rekordbox-mapped row (see ``rb_assets.py``'s ``_local_rb_meta``, which
    already holds ``file_path`` for other fields).

    Mirrors :func:`local_artwork`'s own split instead of collapsing it:
    residency is checked FIRST, so an unresolvable/missing/streaming
    ``file_path`` is a real ``False`` -- no file to read regardless of
    whether a reader exists. Only once a file is confirmed present does a
    missing ``mutagen`` reader become ``None`` ("could not check") rather
    than a guessed ``False``. Collapsing that ``None`` into ``False`` is
    exactly the guessed verdict :func:`local_artwork` refuses to give for
    its own 503 -- this sibling used to make it anyway (#795).
    """
    resolved = _resolve_local_audio_path(file_path)
    if resolved is None:
        return False
    if not HAS_MUTAGEN:
        return None
    from apps.shared import audio_files as _audio_files

    return _audio_files.read_embedded_artwork(resolved) is not None


def local_track_row(stable_id: str) -> tuple[str | None, int | None]:
    """``(file_path, duration_ms)`` from state.db for a track with NO rekordbox
    vendor mapping.

    A locally imported file has a real ``tracks`` row but no djmdContent row,
    so every rekordbox-sourced field (artwork, ANLZ, cues, genre) is absent by
    definition. These two columns are the only honest inputs left for the
    disk-truth flags a browser row still needs, and they are the SAME columns
    :func:`~apps.webui.server.rb_vendor_pkg.track_rows.bulk_availability` falls
    back to for unmapped rows -- so a listing row and its rb-meta can never
    disagree. An unknown stable_id still 404s loudly.
    """
    state = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        row = state.execute(
            "SELECT file_path, duration_ms FROM tracks WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
    finally:
        state.close()
    if row is None:
        raise not_found("TRACK_NOT_FOUND", f"unknown stable_id {stable_id}")
    file_path, duration_ms = row
    return (file_path or None, int(duration_ms) if duration_ms is not None else None)


def resolve_playable_audio(
    stable_id: str, *, share: bool = False
) -> track_locations.PickedAudio:
    """Pick the single file the frontend may play. Never returns a list."""
    folder_path: str | None = None
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
    state = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        exists = state.execute(
            "SELECT 1 FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
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
    ``build_anlz_payload`` exactly -- every array empty, ``vocals``
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
                f"empty-hot-cue:{slot}".encode()
            ).hexdigest(),
        }
        for slot in HOT_CUE_SLOTS
    ]


def artwork_file(content: RbContent, size: str) -> Path:
    """Resolve the artwork jpg for a size variant (s/m/orig), or 404."""
    if size not in config.ARTWORK_FILENAMES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ARTWORK_SIZE_INVALID",
                "message": f"size must be one of {sorted(config.ARTWORK_FILENAMES)}",
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
    derived = _asset_sibling(
        mapped, mapped.resolved.parent / config.ARTWORK_FILENAMES[size]
    )
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


__all__ = [
    "_asset_sibling",
    "anlz_dir",
    "artwork_file",
    "audio_file",
    "empty_anlz_payload",
    "empty_hot_cue_slots",
    "is_streaming_path",
    "local_artwork",
    "local_artwork_available",
    "local_audio_file",
    "local_track_row",
    "resolve_asset_path",
    "resolve_content",
    "resolve_playable_audio",
    "resolve_share_path",
]
