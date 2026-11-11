"""Vendor content lookup and symlink-contained asset resolution.

The resolution chain is unchanged:

    state.db ``tracks`` -> ``track_vendor_ids`` (vendor='rekordbox')
    -> vendor_id -> ``djmdContent`` row in ``(private path)``
    (decrypted copy, opened read-only) -> FolderPath / ImagePath /
    AnalysisDataPath resolved on disk via the share-root rule.

Fail-fast: every unresolved step raises with an explicit ``{"code",
"message"}`` detail; there is no silent None anywhere. The symlink-escape
guards live in :mod:`apps.shared.platform_paths` and are pinned by
``(private path)``, which this move keeps green.

Config constants are read as ``config.<NAME>`` inside function bodies rather
than imported by value, because they are rebindable overrides -- see
:mod:`.config`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from apps.shared import fs_residency, platform_paths
from apps.shared.platform_paths import MappedPath

from . import config
from .errors import _open_ro, not_found
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
    ``(private path)``) -- new code in this module calls
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


def resolve_asset_path(path: str, *, resolver: platform_paths.AssetResolver | None = None) -> MappedPath:
    """Map one vendor asset path and enforce symlink-aware containment.

    ``resolver`` lets one bulk caller (``build_track_rows``) memoise repeat
    containment lookups for the lifetime of that one call -- see
    :class:`apps.shared.platform_paths.AssetResolver`. Every other call site
    omits it and gets the always-uncached behaviour unchanged.
    """
    if resolver is not None:
        return resolver.resolve_asset_path(path)
    return platform_paths.resolve_asset_path(path)


def _asset_sibling(
    mapped: MappedPath,
    candidate: Path,
    *,
    resolver: platform_paths.AssetResolver | None = None,
) -> MappedPath:
    """Contain a derived sibling of an already-mapped vendor asset path."""
    if resolver is not None:
        return resolver.resolve_asset_sibling(mapped, candidate)
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
            f"audio file for track {content.stable_id} is missing or not materialised (dataless/iCloud stub): {path}",
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
    derived = _asset_sibling(mapped, mapped.resolved.parent / config.ARTWORK_FILENAMES[size])
    if derived.resolved is None:
        raise not_found(
            "ARTWORK_NOT_FOUND",
            f"artwork file for track {content.stable_id} is unsafe ({derived.reason}): {content.image_path}",
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
