"""Playlist endpoints + diff viewer -- CAT-05 (+ parity contract items 2/4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_read_state
from ..etag import compute_etag
from ..models import PlaylistDetail, PlaylistDiff, PlaylistSummary, TrackRowOut
from .tracks import AvailableFilter, keep_by_availability

router = APIRouter(prefix="/playlists", tags=["playlists"])


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
    tracks_map = backend.get_tracks_bulk(member_ids)
    available = rb_vendor.bulk_availability(
        member_ids,
        {sid: t.file_path for sid, t in tracks_map.items()},
    )
    return [
        PlaylistSummary(
            playlist_id=pl.playlist_id, name=pl.name, vendor=pl.vendor,
            track_count=len(pl.items),
            available_count=sum(
                1 for sid in pl.items if available.get(sid, False)
            ),
            updated_at=pl.updated_at,
            seq=(
                order.get(pl.vendor_pl_id)
                if pl.vendor == "rekordbox" and pl.vendor_pl_id is not None
                else None
            ),
        )
        for pl in playlists
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
    return PlaylistDetail(
        playlist_id=pl.playlist_id, name=pl.name, vendor=pl.vendor,
        items=list(pl.items),
        tracks=[
            TrackRowOut(**row) for row in rows
            if keep_by_availability(available, row["file_exists"])
        ],
        # GUARD-11: the real differ (apps/sync/playlist_diff.py) only runs
        # offline against copied vendor DBs + a Phase 2 match-set CSV; it is
        # not wired to this read path and has no stable_id-keyed output to
        # serve per request. Rather than fabricate one (the retired
        # fixtures/playlist-diff.json), report the honest "not computed"
        # state -- empty buckets the UI already renders as absent.
        diff=PlaylistDiff(),
    )
