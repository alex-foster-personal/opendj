"""Playlist endpoints + diff viewer -- CAT-05."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends

from ..backend import StateBackend
from ..deps import get_read_state
from ..models import PlaylistDetail, PlaylistDiff, PlaylistSummary

router = APIRouter(prefix="/playlists", tags=["playlists"])

FIXTURE_PATH: Path = (
    Path(__file__).resolve().parent.parent / "fixtures" / "playlist-diff.json"
)


def _load_diff_fixture() -> PlaylistDiff:
    if FIXTURE_PATH.exists():
        data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        return PlaylistDiff(**data)
    return PlaylistDiff()


@router.get("", response_model=list[PlaylistSummary])
def list_playlists(
    backend: StateBackend = Depends(get_read_state),
) -> list[PlaylistSummary]:
    return [
        PlaylistSummary(
            playlist_id=pl.playlist_id, name=pl.name, vendor=pl.vendor,
            track_count=len(pl.items), updated_at=pl.updated_at,
        )
        for pl in backend.list_playlists()
    ]


@router.get("/{playlist_id}", response_model=PlaylistDetail)
def get_playlist(
    playlist_id: str,
    backend: StateBackend = Depends(get_read_state),
) -> PlaylistDetail:
    pl = backend.get_playlist(playlist_id)
    return PlaylistDetail(
        playlist_id=pl.playlist_id, name=pl.name, vendor=pl.vendor,
        items=list(pl.items), diff=_load_diff_fixture(),
    )
