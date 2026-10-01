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
    Paged by ``?limit=`` (1..1000) and ``?offset=``: ``total`` is always the
    whole broken count, ``next_offset`` the offset of the next page or null
    on the last. Whole track rows are read for the returned page only
    (LIBM-136); omit ``limit`` for everything from ``offset`` on.

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

import logging
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from apps.shared.platform_paths import is_unplayable_path
from apps.shared.state.db import open_ro

from .. import library_playable, rb_vendor
from ..backend import MAX_LIMIT, Playlist, StateBackend, Track, TrackFilter
from ..deps import get_read_state
from ..sqlite_backend import SqliteBackend

log = logging.getLogger(__name__)

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
    #: Every broken track the request matches, not the size of this page.
    total: int
    tracks: list[BrokenTrackOut]
    #: Where this page starts in the (title, stable_id) ordering.
    offset: int = 0
    #: ``offset`` of the next page; None when this page reaches the end.
    next_offset: int | None = None


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
    #: Where every live row's audio stands on THIS machine, from the one
    #: shared predicate (``library_playable``) that ingest coverage also
    #: reads, so the two endpoints cannot disagree. Keys: total, present,
    #: broken_here, off_machine, awaiting_volume, streaming, pathless.
    #: ``None`` = unknown (the backend has no state.db to scan), never zero.
    availability: dict[str, int] | None = None


# ----- scan core -------------------------------------------------------------

def _availability(backend: StateBackend) -> dict[str, int] | None:
    if not isinstance(backend, SqliteBackend):
        return None
    conn = open_ro(backend.writeback_state_db_path)
    try:
        return library_playable.scan_playability(conn).counts()
    finally:
        conn.close()


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


@dataclass(frozen=True)
class _BrokenRef:
    """One broken track as the lean scan knows it: enough to count it, order
    it and page it, and nothing that costs a per-track read."""

    stable_id: str
    title: str | None
    original_path: str
    vendor_id: str | None


def _ref_order(ref: _BrokenRef) -> tuple[str, str]:
    return ((ref.title or "").casefold(), ref.stable_id)


def _lean_track_rows(backend: StateBackend) -> list[tuple[str, str | None, str | None]] | None:
    """(stable_id, title, file_path) of every live track, or None when the
    backend is not a state.db with a tracks table."""
    if not isinstance(backend, SqliteBackend):
        return None
    conn = open_ro(backend.writeback_state_db_path)
    try:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tracks'"
        ).fetchone() is None:
            return None
        return [
            (str(sid), title, file_path)
            for sid, title, file_path in conn.execute(
                "SELECT stable_id, title, file_path FROM tracks "
                "WHERE deleted_at IS NULL"
            )
        ]
    finally:
        conn.close()


def _local_refs(rows: list[tuple[str, str | None, str | None]]) -> list[_BrokenRef]:
    """One ref per row whose audio is a local path (rekordbox's folder path wins)."""
    metas = rb_vendor.bulk_rb_meta([sid for sid, _title, _path in rows])
    refs: list[_BrokenRef] = []
    for sid, title, file_path in rows:
        meta = metas.get(sid)
        folder = meta.folder_path if meta is not None else file_path
        if _is_local_path(folder):
            refs.append(_BrokenRef(
                stable_id=sid,
                title=title,
                original_path=str(folder),
                vendor_id=meta.vendor_id if meta is not None else None,
            ))
    return refs


def _scan_broken_refs(backend: StateBackend) -> tuple[int, list[_BrokenRef]]:
    """(total scanned tracks, broken refs sorted by (title, stable_id)).

    The lean scan behind both endpoints. It reads each row's id, title and
    path and nothing else. Reading whole ``Track`` rows through
    ``list_tracks`` also resolved every lane-owned analysis field per track
    (about 265,000 statements on a 9812-row library, 2.8 s of a 3.0 s
    request, measured Thu 1 Oct 2026), and that cost grew as the analysis
    drain filled its tables. Same rows (``deleted_at IS NULL``), same
    predicate and same order as :func:`_scan_broken`.
    """
    rows = _lean_track_rows(backend)
    if rows is None:
        # Not a state.db, or one with no tracks table: the backend's own
        # listing (and its own fallback) decides.
        total, broken = _scan_broken(backend)
        return total, [
            _BrokenRef(b.track.stable_id, b.track.title, b.original_path, b.vendor_id)
            for b in broken
        ]
    candidates = _local_refs(rows)
    exists = rb_vendor.bulk_file_exists(c.original_path for c in candidates)
    broken_refs = [c for c in candidates if not exists[c.original_path]]
    broken_refs.sort(key=_ref_order)
    return len(rows), broken_refs


def _scan_broken_ids(backend: StateBackend) -> tuple[int, set[str]]:
    """(total scanned tracks, broken stable_ids): the summary's scan."""
    total, refs = _scan_broken_refs(backend)
    return total, {ref.stable_id for ref in refs}


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
    limit: int | None = Query(
        None, ge=1, le=MAX_LIMIT,
        description=(
            "Rows in this page. Omit for every row from `offset` on, which "
            "reads whole track rows for all of them and is slow on a large "
            "library."
        ),
    ),
    offset: int = Query(
        0, ge=0, description="Rows to skip in the (title, stable_id) ordering.",
    ),
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> BrokenTrackList:
    if playlist_id is not None:
        # Raises NotFoundError -> 404 via the shared app exception handler,
        # exactly like GET /playlists/{id}.
        wanted = set(backend.get_playlist(playlist_id).items)
    else:
        wanted = set()

    _total_tracks, refs = _scan_broken_refs(backend)
    if playlist_id is not None:
        refs = [ref for ref in refs if ref.stable_id in wanted]

    end = len(refs) if limit is None else min(offset + limit, len(refs))
    page = refs[offset:end]
    # Whole track rows (title, artist, the lane-owned bpm and key) for the
    # page only: the scan above already knows which rows are broken.
    tracks = backend.get_tracks_bulk([ref.stable_id for ref in page])
    member_of = _playlist_membership(backend.list_playlists()) if page else {}
    rows: list[BrokenTrackOut] = []
    for ref in page:
        track = tracks.get(ref.stable_id)
        if track is None:
            # Hard-deleted between the scan and this read: a real race on a
            # live library, and a row that no longer exists is not broken.
            log.warning("reconcile: %s vanished between scan and read", ref.stable_id)
            continue
        rows.append(_to_row(
            _BrokenScan(track=track, original_path=ref.original_path, vendor_id=ref.vendor_id),
            member_of.get(ref.stable_id, []),
        ))
    return BrokenTrackList(
        total=len(refs),
        tracks=rows,
        offset=offset,
        next_offset=end if end < len(refs) else None,
    )


@router.get("/summary", response_model=ReconcileSummary)
def reconcile_summary(
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> ReconcileSummary:
    total_tracks, broken_ids = _scan_broken_ids(backend)
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
        availability=_availability(backend),
    )


__all__ = [
    "BrokenTrackList",
    "BrokenTrackOut",
    "PlaylistBrokenSummary",
    "ReconcileSummary",
    "router",
]
