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
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from apps.cloud import hydration
from apps.cloud import policy as cloud_policy
from apps.cloud.config import CloudConfig, MissingEnvError
from apps.cloud.eviction import HydrationError
from apps.shared import audio_quality, fs_residency, platform_paths
from apps.shared.platform_paths import MappedPath
from apps.shared.state import locations as track_locations
from apps.shared.state import sync_stamp

from . import config
from .cues import HOT_CUE_SLOTS
from .errors import _open_ro, not_found, unavailable
from .models import RbContent

if TYPE_CHECKING:
    from apps.engine_core.jobs.store import JobStore


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


def resolve_asset_path(
    path: str, *, resolver: platform_paths.AssetResolver | None = None
) -> MappedPath:
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
    from apps.shared.state import locations as state_locations

    state = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        row = state.execute(
            "SELECT 1 FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()
        if row is None:
            raise not_found("TRACK_NOT_FOUND", f"unknown stable_id {stable_id}")
        path = state_locations.local_audio_path(state, stable_id)
        canonical = state.execute(
            "SELECT file_path FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()
    finally:
        state.close()
    if path is None:
        raise not_found(
            "AUDIO_FILE_MISSING",
            f"track {stable_id} has no materialised local audio "
            f"(file_path={canonical[0] if canonical else None!r})",
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


def _resolve_local_audio_path(stable_id: str) -> Path | None:
    """Resolved, materialised on-disk path for a state-layer track.

    ``None`` when no local ``track_locations`` row or ``tracks.file_path``
    materialises on this machine -- same residency gate as
    :func:`local_audio_file`. Shared by :func:`local_artwork` and
    :func:`local_artwork_available` so both agree on what "the file exists"
    means, and both check it BEFORE asking whether a reader exists.
    """
    from apps.shared.state import locations as state_locations

    if not config.STATE_DB.exists():
        # No state layer on this machine means no track_locations rows, so
        # nothing is materialised here: a fresh data dir lists every track
        # with artwork_available False rather than answering 500 (#2917
        # follow-up). Same guard as ``track_rows.bulk_rb_meta``.
        return None
    state = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        return state_locations.local_audio_path(state, stable_id)
    finally:
        state.close()


def local_artwork(stable_id: str, *, online: bool = False) -> tuple[bytes, str]:
    """Artwork for a track rekordbox holds none for, or 404 ``ARTWORK_NOT_FOUND``.

    The chain after rekordbox's own jpg, first hit wins:

    1. the picture embedded in the track's local audio file (tinytag, MIT);
    2. a ``cover``/``folder``/``front`` image beside that file;
    3. a cover found online earlier and cached in the app's data dir;
    4. with ``online=True`` only, a MusicBrainz + Cover Art Archive lookup
       now (:mod:`apps.shared.artwork_sources`; ``ODJ_ARTWORK_ONLINE=0``
       turns it off). The listing never asks for this: one lookup per track
       is held to MusicBrainz's one request per second, so only the decks
       ask.

    Never a generated or placeholder image, and never resized: there is no
    pre-rendered s/m variant for these, unlike the rekordbox path. Nothing
    is written into the library or the audio file.
    """
    from apps.shared import artwork_sources
    from apps.shared import audio_files as _audio_files

    file_path, duration_ms = local_track_row(stable_id)
    resolved = _resolve_local_audio_path(stable_id)
    if resolved is not None:
        found = _audio_files.read_embedded_artwork(resolved) or artwork_sources.sidecar_artwork(
            resolved
        )
        if found is not None:
            return found
    cache = online_artwork_cache()
    found = cache.get(stable_id)
    if found is None and online:
        found = artwork_sources.online_cover(cache, stable_id, _track_query(stable_id, duration_ms))
    if found is None:
        raise not_found(
            "ARTWORK_NOT_FOUND",
            f"track {stable_id} has no artwork: no embedded picture, no cover image "
            f"beside the file and no online cover (file_path={file_path!r})",
        )
    return found


def online_artwork_cache() -> Any:
    """The app-data cache of covers found online (never inside the library)."""
    from apps.shared import artwork_sources

    return artwork_sources.ArtworkCache(config.STATE_DB.parent / "artwork-cache")


def _track_query(stable_id: str, duration_ms: int | None) -> Any:
    """Artist + title + duration from state.db for an online lookup."""
    from apps.shared import artwork_sources

    state = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        row = state.execute(
            "SELECT title, artists_json FROM tracks WHERE stable_id = ?", (stable_id,)
        ).fetchone()
    finally:
        state.close()
    title, artists_json = row if row is not None else (None, None)
    artist: str | None = None
    try:
        artists = json.loads(artists_json) if artists_json else []
        if isinstance(artists, list) and artists:
            artist = ", ".join(str(a) for a in artists if a)
        elif isinstance(artists, str):
            artist = artists
    except ValueError:
        artist = None
    return artwork_sources.TrackQuery(artist=artist, title=title, duration_ms=duration_ms)


def local_artwork_available(stable_id: str) -> bool | None:
    """Whether ``/artwork`` would serve something for a track with no
    rekordbox mapping, without going online: an embedded picture, a cover
    image beside the file, or an online cover already cached. Still typed
    ``bool | None`` for the wire, but the reader (tinytag) is now a core
    dependency, so it never answers ``None`` ("could not check") any more
    (#795, #4717).
    """
    return _artwork_available_for_resolved(stable_id, _resolve_local_audio_path(stable_id))


def bulk_local_artwork_available(
    state: sqlite3.Connection, stable_ids: Sequence[str],
) -> dict[str, bool | None]:
    """:func:`local_artwork_available` for many rows over ONE open state.db.

    The listing hot path (issue #3962): the per-row function opens a fresh
    read-only connection and re-runs the locations schema probes for every
    row, which at 10,000 folder-imported tracks was 30 s to open one playlist.
    This resolves every path through
    :func:`apps.shared.state.locations.bulk_local_audio_paths`, whose
    candidate order is the per-row resolver's, then applies the SAME
    residency-first verdict, so a listed row and its rb-meta never disagree.
    The caller owns ``state`` and has already established that state.db
    exists (the per-row function answers False without one).
    """
    resolved = track_locations.bulk_local_audio_paths(state, stable_ids)
    return {sid: _artwork_available_for_resolved(sid, resolved[sid]) for sid in stable_ids}


def _artwork_available_for_resolved(stable_id: str, resolved: Path | None) -> bool:
    """Whether :func:`local_artwork` would serve something without going online."""
    from apps.shared import artwork_sources
    from apps.shared import audio_files as _audio_files

    if resolved is not None and (
        _audio_files.embedded_artwork_available(resolved)
        or artwork_sources.sidecar_artwork_path(resolved) is not None
    ):
        return True
    return online_artwork_cache().has(stable_id)


def local_track_row(stable_id: str) -> tuple[str | None, int | None]:
    """``(file_path, duration_ms)`` from state.db for a track with NO rekordbox
    vendor mapping.

    A locally imported file has a real ``tracks`` row but no djmdContent row,
    so every rekordbox-sourced field (artwork, ANLZ, cues) is absent by
    definition. Genre and comment are state-layer file-tag facts resolved by
    the web read model. These two columns are the only honest inputs left for the
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


def local_track_file_tags(stable_id: str) -> tuple[str | None, str | None]:
    """``(genre, comment)`` import-time file tags for an unmapped track.

    A folder import persists the genre and comment it read off the file's
    own tags into ``track_fields`` (see
    :func:`apps.shared.state.ingest.folder._write_file_tag_metadata`). They
    are file facts, not rekordbox facts, so they are the only two metadata
    fields an unmapped row can honestly serve.

    Read through the SAME ``config.STATE_DB`` connection
    :func:`local_track_row` uses rather than through the request's
    ``StateBackend``: the two resolve independently, and a local-only
    library whose ``state.db`` the backend never opened would otherwise
    answer "track not found" for a row this module can see. Absent tags
    (no import, or a file with none) return ``None``, never an invented
    value.
    """
    state = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        rows = state.execute(
            "SELECT field_name, value_json FROM track_fields "
            "WHERE stable_id = ? AND field_name IN ('genre', 'comments')",
            (stable_id,),
        ).fetchall()
    finally:
        state.close()
    tags: dict[str, str] = {}
    for field_name, value_json in rows:
        try:
            value = json.loads(value_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, str) and value:
            tags[field_name] = value
    return (tags.get("genre"), tags.get("comments"))


def _picked_from_path(path: Path, *, source: str) -> track_locations.PickedAudio:
    media_type = config.AUDIO_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise not_found(
            "AUDIO_FILE_MISSING",
            f"unsupported audio extension for {path}",
        )
    quality = audio_quality.classify(str(path), None)
    return track_locations.PickedAudio(
        path=path,
        media_type=media_type,
        kind="local",
        venue_key=quality.venue.key if quality.venue else None,
        venue_rank=quality.venue.rank if quality.venue else None,
        source=source,
    )


def resolve_playable_audio(
    stable_id: str,
    *,
    share: bool = False,
    jobs_store: JobStore | None = None,
) -> track_locations.PickedAudio:
    """Pick the single file the frontend may play via believed-state resolution.

    Believed state answers WHETHER this machine may serve audio at all (its own
    copy, a hydrated cache entry, or not yet). For a share host it does not
    answer WHICH copy: a remote audience is capped at the share venue ceiling,
    so when several local copies exist the cap picks among them.
    """
    share_policy = track_locations.policy_from_env(share=True) if share else None
    if not share:
        from apps.shared.library_mode import library_mode

        library_mode()
    state = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        exists = state.execute(
            "SELECT 1 FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()
        if exists is None:
            raise not_found("TRACK_NOT_FOUND", f"unknown stable_id {stable_id}")
        machine_id = sync_stamp.local_machine_id(state)
        cache_dir = cloud_policy.artifact_cache_root(config.DATA_DIR, "audio")
        try:
            cfg = CloudConfig.from_env()
        except MissingEnvError:
            cfg = None
        try:
            source = hydration.resolve_playback_source(
                state,
                stable_id,
                machine_id,
                asset_kind="audio",
                cache_dir=cache_dir,
                cfg=cfg,
            )
        except HydrationError as exc:
            raise unavailable(
                "CLOUD_POLICY_UNCONFIGURED",
                str(exc),
            ) from exc
        # A cache entry is the one hydrated object, so there is nothing for the
        # cap to choose between; only a machine serving its own copies can hold
        # a master and a lossy alternate of the same track.
        share_pick = (
            track_locations.pick_playable(state, stable_id, policy=share_policy)
            if share_policy is not None and source.origin == "local"
            else None
        )
    finally:
        state.close()

    if source.origin in ("local", "cache"):
        if source.path is None:
            raise not_found(
                "AUDIO_FILE_MISSING",
                f"believed-state origin {source.origin!r} has no path for {stable_id}",
            )
        if share_pick is not None:
            # The capped pick reads the same two layers believed state does
            # (track_locations, then tracks.file_path), so None here is not a
            # missing file: it is the picker's stricter open probe rejecting a
            # path the believed-state residency gate admits, a FIFO the audio
            # route probes itself (#766, #2749). That path then stands as-is.
            return share_pick
        return _picked_from_path(
            source.path,
            source="believed-state-local"
            if source.origin == "local"
            else "believed-state-cache",
        )

    if source.origin == "unavailable":
        if source.policy_source == "unconfigured":
            # CLOUDSYNC-33: a local-only machine without this file. The
            # message is the one a DJ reads on the deck, so it is plain.
            raise not_found(
                "AUDIO_NOT_ON_THIS_MACHINE",
                source.reason or hydration.NOT_ON_THIS_MACHINE,
            )
        raise not_found(
            "CLOUD_ASSET_UNAVAILABLE",
            source.reason
            or f"audio for track {stable_id} is unavailable on this machine",
        )

    if source.origin == "presigned":
        try:
            hydration.enqueue_hydrate_asset(
                jobs_store,
                stable_id=stable_id,
                asset_kind="audio",
                machine_id=machine_id,
                data_dir=config.DATA_DIR,
            )
        except HydrationError as exc:
            raise unavailable("CLOUD_HYDRATING", str(exc)) from exc
        raise unavailable(
            "CLOUD_HYDRATING",
            f"audio for track {stable_id} is hydrating from remote storage",
        )

    raise not_found(
        "AUDIO_FILE_MISSING",
        f"no working audio location for track {stable_id}",
    )


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
        # `source` is REQUIRED on every /anlz beatgrid block, own or rekordbox
        # (NATIVE-01): a consumer must never have to infer which producer it is
        # looking at. A locally imported file has no rekordbox grid, but this
        # IS the rekordbox branch, and "rekordbox with no beats" is what the
        # empty arrays already say.
        "beatgrid": {"source": "rekordbox", "beat_count": 0, "beats": []},
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
    "bulk_local_artwork_available",
    "empty_anlz_payload",
    "empty_hot_cue_slots",
    "is_streaming_path",
    "local_artwork",
    "local_artwork_available",
    "local_audio_file",
    "local_track_file_tags",
    "local_track_row",
    "resolve_asset_path",
    "resolve_content",
    "resolve_playable_audio",
    "resolve_share_path",
]
