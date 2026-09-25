"""Read-only reconcile endpoints -- data side of node ``missing-tracks-folder``.

LANE reconcile-router (single owner). ``relocate-files`` and the Missing
Tracks UIs build against THIS contract; the wave integrator wires the router.

Integrator wiring (one line each, in apps/webui/server/app.py):

    from .routes import reconcile as reconcile_routes
    app.include_router(reconcile_routes.router, prefix=api_prefix)

Endpoints
---------

``GET /api/v1/reconcile/broken`` -> :class:`BrokenTrackList`
    Every library track whose recorded local audio path no longer resolves
    on disk. Optional ``?playlist_id=`` narrows to one playlist's broken
    members (404 ``PLAYLIST_NOT_FOUND`` style via the shared NotFoundError
    handler when the playlist does not exist). Rows are sorted by
    (title, stable_id) and carry ``playlist_ids`` (every playlist that
    references the track) plus ``vendor_id`` (rekordbox djmdContent ID when
    mapped -- what relocate-files needs to patch FolderPath later).

``GET /api/v1/reconcile/summary`` -> :class:`ReconcileSummary`
    Library-wide broken count plus per-playlist counts, one call:
    ``total_broken`` (badge for the Missing Tracks folder row),
    ``orphan_broken`` (broken tracks in no playlist), and a
    ``playlists`` list with ``track_count`` / ``broken_count`` for every
    playlist (zero counts included so the tree can badge everything).

Availability semantics (mirror of ``apps.reconcile.list_broken``)
-----------------------------------------------------------------

A track is BROKEN iff all three hold, exactly like
``list_broken._collect_broken`` (skip ``is_streaming``, skip ``file_path is
None``, keep ``not file_path.exists()``):

  1. it has a recorded path: rekordbox ``FolderPath`` when a rekordbox
     vendor mapping exists (:func:`rb_vendor.bulk_rb_meta`), else the
     state-layer ``file_path``;
  2. that path is local -- not a streaming URI. Streaming detection reuses
     the reconcile app's own helper
     (:func:`apps.shared.platform_paths.is_unplayable_path`: spotify:/tidal:/
     http(s), plus empty) unioned with the webui prefixes
     (:func:`rb_vendor.is_streaming_path`: tidal:/soundcloud:/spotify:);
  3. the path does not exist on disk -- disk truth via
     :func:`rb_vendor.bulk_file_exists` (share-root aware, TTL'd stat
     cache, never a per-row stat fan-out).

Streaming rows and pathless rows are NOT broken -- they have no local file
to lose. Read-only: state.db and master.plain.db are only ever opened
read-only inside rb_vendor; this module never writes anything.

Frontend contract for the Missing Tracks folder row (tree wiring is the
browser/smartlists units' territory -- do not edit tree components here):
render a pseudo-folder row labelled "Missing Tracks" whose count badge is
``summary.total_broken`` and whose click handler lists
``GET /api/v1/reconcile/broken`` rows (``file_exists`` is always false,
``is_streaming`` always false, so existing track-row renderers can reuse
their unavailable styling unchanged).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from apps.shared.platform_paths import is_unplayable_path

from .. import rb_vendor
from ..backend import MAX_LIMIT, Playlist, StateBackend, Track, TrackFilter
from ..deps import get_read_state

router = APIRouter(prefix="/reconcile", tags=["reconcile"])

# Full-library scan guard: 1000 pages x MAX_LIMIT (1000) = 1M tracks. A scan
# that deep means a pagination bug (cursor not advancing) -- fail loudly.
_MAX_SCAN_PAGES: int = 1000


# ----- response models (route-local: models.py is a hotspot file) -----------

class BrokenTrackOut(BaseModel):
    """One broken track row. Field names align with ``TrackRowOut`` where the
    concepts overlap (title/artist/key/bpm/rating/duration_ms/file_exists/
    is_streaming) and with ``apps.reconcile.list_broken.CSV_COLUMNS`` for the
    reconcile-specific path fields (original_path/basename/parent_dir)."""

    stable_id: str
    title: str | None
    artist: str | None
    album: str | None
    bpm: float | None
    key: str | None
    rating: int | None
    duration_ms: int | None
    original_path: str
    basename: str
    parent_dir: str
    # rekordbox djmdContent ID when a vendor mapping exists, else None
    # (state-layer-only track). relocate-files patches FolderPath by this ID.
    vendor_id: str | None
    playlist_ids: list[str]
    file_exists: bool = False
    is_streaming: bool = False


class BrokenTrackList(BaseModel):
    total: int
    tracks: list[BrokenTrackOut]


class PlaylistBrokenSummary(BaseModel):
    playlist_id: str
    name: str
    vendor: str
    track_count: int
    broken_count: int


class ReconcileSummary(BaseModel):
    total_tracks: int
    total_broken: int
    # Broken tracks referenced by no playlist -- only reachable via the
    # Missing Tracks folder, never via a playlist badge.
    orphan_broken: int
    playlists: list[PlaylistBrokenSummary]


# ----- scan core -------------------------------------------------------------

@dataclass(frozen=True)
class _BrokenScan:
    """Internal scan result: the backend track + its resolved dead path."""

    track: Track
    original_path: str
    vendor_id: str | None


def _iter_all_tracks(backend: StateBackend) -> list[Track]:
    """Drain the paginated track listing (cursor loop, fail-fast bound)."""
    tracks: list[Track] = []
    cursor: str | None = None
    for _ in range(_MAX_SCAN_PAGES):
        page = backend.list_tracks(TrackFilter(cursor=cursor, limit=MAX_LIMIT))
        tracks.extend(page.items)
        if page.next_cursor is None:
            return tracks
        cursor = page.next_cursor
    raise HTTPException(status_code=500, detail={
        "code": "RECONCILE_SCAN_OVERFLOW",
        "message": (
            f"track scan exceeded {_MAX_SCAN_PAGES} pages of {MAX_LIMIT}; "
            "cursor pagination is not advancing"
        ),
    })


def _is_local_path(path: str | None) -> bool:
    """True iff ``path`` is a real local file path (reconcile-app semantics).

    ``is_unplayable_path`` already treats empty/None as unplayable, which
    matches list_broken skipping ``file_path is None`` rows.
    """
    if is_unplayable_path(path):
        return False
    if rb_vendor.is_streaming_path(path):
        return False
    return True


def _scan_broken(backend: StateBackend) -> tuple[int, list[_BrokenScan]]:
    """(total scanned tracks, broken tracks sorted by (title, stable_id)).
    See module docstring for the exact availability predicate."""
    tracks = _iter_all_tracks(backend)
    metas = rb_vendor.bulk_rb_meta([t.stable_id for t in tracks])

    candidates: list[_BrokenScan] = []
    for track in tracks:
        meta = metas.get(track.stable_id)
        folder = meta.folder_path if meta is not None else track.file_path
        if not _is_local_path(folder):
            continue
        assert folder is not None  # _is_local_path rejects empty/None
        candidates.append(_BrokenScan(
            track=track,
            original_path=folder,
            vendor_id=meta.vendor_id if meta is not None else None,
        ))

    exists = rb_vendor.bulk_file_exists(c.original_path for c in candidates)
    broken = [c for c in candidates if not exists[c.original_path]]
    broken.sort(key=lambda c: ((c.track.title or "").casefold(), c.track.stable_id))
    return len(tracks), broken


def _playlist_membership(
    playlists: list[Playlist],
) -> dict[str, list[str]]:
    """stable_id -> [playlist_id, ...] in stable playlist order."""
    member_of: dict[str, list[str]] = {}
    for pl in sorted(playlists, key=lambda p: p.playlist_id):
        for sid in pl.items:
            member_of.setdefault(sid, []).append(pl.playlist_id)
    return member_of


def _to_row(scan: _BrokenScan, playlist_ids: list[str]) -> BrokenTrackOut:
    t = scan.track
    original = scan.original_path
    return BrokenTrackOut(
        stable_id=t.stable_id,
        title=t.title,
        artist=t.artist,
        album=t.album,
        bpm=t.bpm,
        key=t.key,
        rating=t.rating,
        duration_ms=t.duration_ms,
        original_path=original,
        basename=Path(original).name,
        parent_dir=str(Path(original).parent),
        vendor_id=scan.vendor_id,
        playlist_ids=playlist_ids,
    )


# ----- endpoints -------------------------------------------------------------

@router.get("/broken", response_model=BrokenTrackList)
def list_broken_tracks(
    playlist_id: str | None = Query(
        None,
        description=(
            "Restrict to broken members of one playlist (404 when the "
            "playlist does not exist). Omit for the library-wide listing."
        ),
    ),
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> BrokenTrackList:
    if playlist_id is not None:
        # Raises NotFoundError -> 404 via the shared app exception handler,
        # exactly like GET /playlists/{id}.
        wanted = set(backend.get_playlist(playlist_id).items)
    else:
        wanted = set()

    _total_tracks, broken = _scan_broken(backend)
    if playlist_id is not None:
        broken = [b for b in broken if b.track.stable_id in wanted]

    member_of = _playlist_membership(backend.list_playlists())
    rows = [_to_row(b, member_of.get(b.track.stable_id, [])) for b in broken]
    return BrokenTrackList(total=len(rows), tracks=rows)


@router.get("/summary", response_model=ReconcileSummary)
def reconcile_summary(
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> ReconcileSummary:
    total_tracks, broken = _scan_broken(backend)
    broken_ids = {b.track.stable_id for b in broken}
    playlists = backend.list_playlists()

    per_playlist = [
        PlaylistBrokenSummary(
            playlist_id=pl.playlist_id,
            name=pl.name,
            vendor=pl.vendor,
            track_count=len(pl.items),
            broken_count=sum(1 for sid in pl.items if sid in broken_ids),
        )
        for pl in playlists
    ]
    per_playlist.sort(key=lambda p: (-p.broken_count, p.name.casefold(), p.playlist_id))

    referenced = {sid for pl in playlists for sid in pl.items}
    orphan_broken = sum(1 for sid in broken_ids if sid not in referenced)
    return ReconcileSummary(
        total_tracks=total_tracks,
        total_broken=len(broken_ids),
        orphan_broken=orphan_broken,
        playlists=per_playlist,
    )


__all__ = [
    "BrokenTrackList",
    "BrokenTrackOut",
    "PlaylistBrokenSummary",
    "ReconcileSummary",
    "router",
]
