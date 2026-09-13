"""Playlist endpoints + diff viewer -- CAT-05 (+ parity contract items 2/4)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from apps.shared.state import db as state_db

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_read_state
from ..etag import compute_etag
from ..models import PlaylistDetail, PlaylistDiff, PlaylistSummary, TrackRowOut
from .tracks import AvailableFilter, keep_by_availability

router = APIRouter(prefix="/playlists", tags=["playlists"])


class DeletedPlaylistOut(BaseModel):
    playlist_id: str
    name: str
    vendor: str
    vendor_pl_id: str
    deleted_at: str
    updated_at: str
    track_count: int


_DELETED_PLAYLISTS_SQL = """
SELECT p.playlist_id, p.name, p.vendor, p.vendor_pl_id,
       p.deleted_at, p.updated_at,
       (SELECT COUNT(*) FROM playlist_memberships m
        WHERE m.playlist_id = p.playlist_id
          AND m.deleted_at = p.deleted_at) AS track_count
FROM playlists p
WHERE p.deleted_at IS NOT NULL
ORDER BY p.deleted_at DESC, p.playlist_id
"""


@router.get("", response_model=list[PlaylistSummary])
def list_playlists(
    backend: StateBackend = Depends(get_read_state),
) -> list[PlaylistSummary]:
    playlists = backend.list_playlists()
    # Rekordbox playlists carry the user's custom tree order (djmdPlaylist
    # ParentID/Seq - SCREENSHOT-SPEC 5b); resolve it once for the whole list.
    # Only consult Rekordbox when at least one row can actually participate in
    # vendor ordering. Imported/test rows without vendor IDs have no order to
    # resolve and must not make an otherwise self-contained backend require
    # master.plain.db.
    order: dict[str, int] = {}
    if any(
        pl.vendor == "rekordbox" and pl.vendor_pl_id is not None
        for pl in playlists
    ):
        order = rb_vendor.playlist_order_index()

    # available_count (FR-1 item 4): one bulk vendor lookup + one cached
    # stat pass across every member of every playlist -- never a per-row
    # stat fan-out. A membership pointing at a stable_id with no track row
    # counts as unavailable (it is certainly not playable from disk).
    member_ids = sorted({sid for pl in playlists for sid in pl.items})
    # get_file_paths_bulk, not get_tracks_bulk: available_count only ever
    # reads .file_path, and hydrating a full Track (EAV pass included) per
    # member for a field the summary discards was ~37% of this route's wall
    # time (pin e0f3a90652a9, measured Sat 5 Sep 2026 against the real
    # library: 7155 unique members).
    file_paths = backend.get_file_paths_bulk(member_ids)
    available = rb_vendor.bulk_availability(member_ids, file_paths)
    return [
        PlaylistSummary(
            playlist_id=pl.playlist_id, name=pl.name, vendor=pl.vendor,
            track_count=len(pl.items),
            available_count=sum(
                1 for sid in pl.items if available.get(sid, False)
            ),
            updated_at=pl.updated_at,
            forbid_duplicates=pl.forbid_duplicates,
            seq=(
                order.get(pl.vendor_pl_id)
                if pl.vendor == "rekordbox" and pl.vendor_pl_id is not None
                else None
            ),
        )
        for pl in playlists
    ]


@router.get(
    "/deleted",
    response_model=list[DeletedPlaylistOut],
    operation_id="list_deleted_playlists",
)
def list_deleted_playlists(
    request: Request,
    _backend: StateBackend = Depends(get_read_state),
) -> list[DeletedPlaylistOut]:
    db_path = Path(getattr(request.app.state, "state_db_path", "data/state/state.db"))
    if not db_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "state_db_missing",
                "message": f"state DB not found at {db_path}",
            },
        )
    conn = state_db.open_ro(db_path)
    try:
        rows = conn.execute(_DELETED_PLAYLISTS_SQL).fetchall()
    finally:
        conn.close()
    return [
        DeletedPlaylistOut(
            playlist_id=row[0],
            name=row[1],
            vendor=row[2],
            vendor_pl_id=row[3],
            deleted_at=row[4],
            updated_at=row[5],
            track_count=row[6],
        )
        for row in rows
    ]


@router.get("/{playlist_id}", response_model=PlaylistDetail)
def get_playlist(
    playlist_id: str,
    response: Response,
    available: AvailableFilter = Query(
        "all",
        description=(
            "Filter the hydrated `tracks` rows on file_exists disk truth "
            "(FR-1 agent parity). `items` always stays the full membership."
        ),
    ),
    backend: StateBackend = Depends(get_read_state),
) -> PlaylistDetail:
    pl = backend.get_playlist(playlist_id)
    # add-remove-reorder-tracks: the write side's PUT .../tracks requires
    # If-Match (etag.py's compute_etag(playlist_id, updated_at), identical
    # to playlist_store.PlaylistRow.etag) -- expose it here so a client that
    # only ever reads through this route can still mutate membership.
    response.headers["ETag"] = compute_etag(pl.playlist_id, pl.updated_at)
    # Hydrated rows in membership order (parity contract item 4) -- kills
    # the 29x per-row GET fan-out the old client-side join needed.
    tracks_map = backend.get_tracks_bulk(pl.items)
    missing = [sid for sid in pl.items if sid not in tracks_map]
    if missing:
        # Dangling membership = corrupt state.db; fail loudly, never render
        # invented placeholder rows.
        raise HTTPException(status_code=500, detail={
            "code": "PLAYLIST_MEMBER_MISSING",
            "message": (
                f"playlist {playlist_id} references {len(missing)} stable_ids "
                f"with no track row (first: {missing[:5]})"
            ),
        })
    rows = rb_vendor.build_track_rows([tracks_map[sid] for sid in pl.items])
    item_ids = list(pl.item_ids or [])
    tracks: list[TrackRowOut] = []
    for i, row in enumerate(rows):
        iid = item_ids[i] if i < len(item_ids) else None
        out = TrackRowOut(**row, item_id=iid or None)
        if keep_by_availability(available, row["file_exists"]):
            tracks.append(out)
    return PlaylistDetail(
        playlist_id=pl.playlist_id, name=pl.name, vendor=pl.vendor,
        forbid_duplicates=pl.forbid_duplicates,
        items=list(pl.items),
        tracks=tracks,
        # GUARD-11: the real differ (apps/sync/playlist_diff.py) only runs
        # offline against copied vendor DBs + a Phase 2 match-set CSV; it is
        # not wired to this read path and has no stable_id-keyed output to
        # serve per request. Rather than fabricate one (the retired
        # fixtures/playlist-diff.json), report the honest "not computed"
        # state -- empty buckets the UI already renders as absent.
        diff=PlaylistDiff(),
    )
