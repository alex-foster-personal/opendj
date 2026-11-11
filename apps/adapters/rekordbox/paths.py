"""Stable compatibility surface for Rekordbox path and playback resolution.

Vendor content and asset primitives live in :mod:`.content_paths`; playback
selection and CloudSync policy live in :mod:`.playable_audio`. Historical
imports stay here alongside state-backed local artwork, payload and USB helpers.
All modules share call-time ``config.<NAME>`` overrides.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from apps.shared.state import locations as track_locations
from apps.sync.usb.stick_model import is_canonical_volume_uuid

from . import config
from .content_paths import (
    _asset_sibling,
    anlz_dir,
    artwork_file,
    audio_file,
    is_streaming_path,
    resolve_asset_path,
    resolve_content,
    resolve_share_path,
)
from .cues import HOT_CUE_SLOTS
from .errors import _open_ro, not_found
from .models import VendorUsbMention
from .playable_audio import (
    _rekordbox_copy,  # noqa: F401 - historical private import surface
    resolve_playable_audio,
)


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


def _local_artwork_source(
    stable_id: str,
    resolved: Path | None,
    file_path: str | None,
    duration_ms: int | None,
    *,
    online: bool,
) -> tuple[bytes, str, bool]:
    """Resolve raw artwork bytes; the bool is True when the source was embedded-only."""
    from apps.shared import artwork_sources
    from apps.shared import audio_files as _audio_files

    if resolved is not None:
        embedded = _audio_files.read_embedded_artwork(resolved)
        if embedded is not None:
            return (*embedded, True)
        sidecar = artwork_sources.sidecar_artwork(resolved)
        if sidecar is not None:
            return (*sidecar, False)
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
    return (*found, False)


def _sized_local_artwork(
    stable_id: str,
    resolved: Path | None,
    file_path: str | None,
    duration_ms: int | None,
    size: str,
    *,
    online: bool,
) -> tuple[bytes, str]:
    from apps.shared import artwork_resize

    cache = resized_artwork_cache()
    identity: str | None = None
    if resolved is not None:
        identity = artwork_resize.source_identity(stable_id, resolved, size)
        if identity is not None:
            hit = cache.indexed(identity)
            if hit is not None:
                return hit, "image/jpeg"

    data, mime, from_embedded = _local_artwork_source(
        stable_id, resolved, file_path, duration_ms, online=online
    )
    out = cache.sized(data, size)
    if out is None:
        return data, mime
    if from_embedded and identity is not None:
        cache.remember(identity, data, size)
    return out, "image/jpeg"


def local_artwork(
    stable_id: str, *, online: bool = False, size: str = "orig",
) -> tuple[bytes, str]:
    """Artwork for a track rekordbox holds none for, or 404 ``ARTWORK_NOT_FOUND``.

    The chain after rekordbox's own jpg, first hit wins:

    1. the picture embedded in the track's local audio file (tinytag, MIT,
       via :mod:`apps.shared.tag_reader`, a core dependency that ships in
       every build);
    2. a ``cover``/``folder``/``front`` image beside that file;
    3. a cover found online earlier and cached in the app's data dir;
    4. with ``online=True`` only, a MusicBrainz + Cover Art Archive lookup
       now (:mod:`apps.shared.artwork_sources`; ``ODJ_ARTWORK_ONLINE=0``
       turns it off). The listing never asks for this: one lookup per track
       is held to MusicBrainz's one request per second, so only the decks
       ask.

    Never a generated or placeholder image. For ``size`` ``s``/``m``, the
    longest edge is scaled down (never up) to 80 or 240 px, JPEG, with alpha
    flattened onto the slate tile colour ``#22262c``. Renders are cached under
    ``<state dir>/artwork-resized/`` keyed by the source bytes' sha256 and
    ``size``; a repeat request for an embedded cover reads the cache after
    one stat of the audio file, without re-reading the tags. ``orig`` is the
    source as found. Nothing is written into the library or the audio file.
    """
    file_path, duration_ms = local_track_row(stable_id)
    resolved = _resolve_local_audio_path(stable_id)
    if size == "orig":
        data, mime, _from_embedded = _local_artwork_source(
            stable_id, resolved, file_path, duration_ms, online=online
        )
        return data, mime
    return _sized_local_artwork(
        stable_id, resolved, file_path, duration_ms, size, online=online
    )


def resized_artwork_cache() -> Any:
    """Content-addressed JPEG cache for ``local_artwork`` s/m sizes (under state dir)."""
    from apps.shared import artwork_resize

    return artwork_resize.ResizedArtworkCache(
        config.STATE_DB.parent / artwork_resize.CACHE_DIR_NAME,
    )


def online_artwork_cache() -> Any:
    """The app-data cache of covers found online (never inside the library)."""
    from apps.shared import artwork_sources

    return artwork_sources.ArtworkCache(config.STATE_DB.parent / "artwork-cache")


def online_track_query(stable_id: str) -> Any:
    """The :class:`~apps.shared.artwork_sources.TrackQuery` an online lookup sends for a track."""
    _file_path, duration_ms = local_track_row(stable_id)
    return _track_query(stable_id, duration_ms)


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


def local_artwork_available(stable_id: str) -> bool:
    """Whether ``/artwork`` would serve something for a track with no
    rekordbox mapping, without going online: an embedded picture, a cover
    image beside the file, or an online cover already cached. The reader
    (tinytag, via :mod:`apps.shared.tag_reader`) is a core dependency, so
    this never answers "could not check" (#795, #4717).
    """
    return _artwork_available_for_resolved(stable_id, _resolve_local_audio_path(stable_id))


def bulk_local_artwork_available(
    state: sqlite3.Connection, stable_ids: Sequence[str],
) -> dict[str, bool]:
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
    :func:`apps.shared.state.ingest.folder.write_file_tag_metadata`). They
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


def list_rekordbox_usb_mentions(master_db: Path) -> list[VendorUsbMention]:
    """Read djmdDevice rows that name a removable USB stick (UUID-keyed).

    Returns an empty list when the file is missing or unreadable. Never writes.
    """
    path = Path(master_db)
    if not path.is_file():
        return []
    try:
        master = _open_ro(path, "MASTER_DB")
    except HTTPException:
        return []
    try:
        rows = master.execute(
            "SELECT ID, Name, UUID FROM djmdDevice "
            "WHERE rb_local_deleted = 0 AND Name IS NOT NULL AND Name != ''"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        master.close()

    out: list[VendorUsbMention] = []
    for device_id, name, raw_uuid in rows:
        device_id_s = str(device_id).strip()
        label = str(name).strip()
        if not label:
            continue
        uuid: str | None = None
        if raw_uuid is not None:
            candidate = str(raw_uuid).strip().upper()
            if is_canonical_volume_uuid(candidate):
                uuid = candidate
        out.append(
            VendorUsbMention(
                name=label,
                source="rekordbox",
                volume_uuid=uuid,
                device_id=device_id_s or None,
            )
        )
    return out


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
    "list_rekordbox_usb_mentions",
    "local_track_row",
    "resized_artwork_cache",
    "resolve_asset_path",
    "resolve_content",
    "resolve_playable_audio",
    "resolve_share_path",
]
