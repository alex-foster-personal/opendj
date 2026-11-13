"""Play history routes (logged deck loads), rekordbox History parity (PLAYS, issue #9745).

One history day runs from local 06:00 on that calendar day until 06:00 the next
day (``play_log.HISTORY_DAY_ROLLOVER_HOUR``), so a set running past midnight
stays one list. rekordbox splits History by session; Open DJ splits by night
because a play log has no session marker and a DJ thinks of "Friday's set".

- ``GET /history`` lists the days that hold plays, newest first, with counts.
- ``GET /history/{day}`` returns that day's plays in play order with ``played_at``.
- ``POST /history/{day}/playlist`` saves that day as a normal playlist, tracks in
  play order (a track played twice appears twice, as in rekordbox History).
- ``DELETE /history/{day}`` removes every Open DJ play in that day's window.

The optional ``tz`` query is an IANA zone name; when omitted the server's local
zone is used (the desktop app runs colocated with the browser). This module is
not ``playlist_history.py``, which is playlist navigation history.

Mounted from ``app_wiring.py`` under the API prefix.
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state import play_log

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state
from ..playlist_store import PlaylistStore
from .playlist_write import get_playlist_store
from .track_plays import _state_db_path

router = APIRouter(prefix="/history", tags=["play-history"])
MOUNT_UNDER_API_PREFIX = True


_TZ_QUERY = Query(
    default=None,
    description="IANA timezone for the 06:00 day boundary; default is server local.",
)


class HistoryDayOut(BaseModel):
    date: str = Field(description="YYYY-MM-DD: the night that starts at 06:00 local on this date")
    count: int = Field(description="Plays logged in this day's window")


class HistoryDaysOut(BaseModel):
    rollover_hour: int = Field(description="Local hour at which one history day becomes the next")
    days: list[HistoryDayOut] = Field(description="Days that hold plays, newest first")


class HistoryPlayOut(BaseModel):
    position: int = Field(description="1-based play order within the day")
    stable_id: str
    play_id: str | None
    played_at: str = Field(description="Canonical UTC time of the play")
    deck: int | None
    source: str = Field(description="'opendj', or the app the play was imported from")


class HistoryDayPlaysOut(BaseModel):
    date: str
    start: str = Field(description="Canonical UTC bound used (inclusive)")
    end: str = Field(description="Canonical UTC bound used (exclusive)")
    plays: list[HistoryPlayOut]


class HistoryPlaylistIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Playlist name; default 'History YYYY-MM-DD'.",
    )


class HistoryPlaylistOut(BaseModel):
    playlist_id: str
    name: str
    track_count: int = Field(description="Rows in the new playlist (one per play, repeats kept)")
    skipped: list[str] = Field(description="stable_ids played that day but no longer in the collection, left out")


def _zone(tz: str | None) -> ZoneInfo | None:
    """The IANA zone named by ``tz`` (None = server local); 422 BAD_TZ when unknown."""
    if tz is None:
        return None
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "BAD_TZ", "message": f"unknown timezone {tz!r}"},
        ) from error


def _day_plays(request: Request, day: date, tz: str | None) -> HistoryDayPlaysOut:
    start, end = play_log.history_day_bounds(day, _zone(tz))
    conn = state_db.open_ro(_state_db_path(request))
    try:
        rows = play_log.plays_since(conn, start, end)
    finally:
        conn.close()
    return HistoryDayPlaysOut(
        date=day.isoformat(),
        start=start,
        end=end,
        plays=[
            HistoryPlayOut(
                position=index,
                stable_id=row.stable_id,
                play_id=row.play_id,
                played_at=row.played_at,
                deck=row.deck,
                source=row.source,
            )
            for index, row in enumerate(rows, start=1)
        ],
    )


def _live_stable_ids(request: Request, stable_ids: list[str]) -> set[str]:
    unique = list(dict.fromkeys(stable_ids))
    if not unique:
        return set()
    conn = state_db.open_ro(_state_db_path(request))
    try:
        placeholders = ",".join("?" * len(unique))
        return {
            str(sid)
            for (sid,) in conn.execute(
                f"SELECT stable_id FROM tracks WHERE deleted_at IS NULL AND stable_id IN ({placeholders})",
                unique,
            )
        }
    finally:
        conn.close()


@router.get("", response_model=HistoryDaysOut)
def list_history_days(
    request: Request,
    tz: str | None = _TZ_QUERY,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> HistoryDaysOut:
    """Days that hold plays, newest first, each with its play count (rekordbox History list)."""
    zone = _zone(tz)
    conn = state_db.open_ro(_state_db_path(request))
    try:
        days = play_log.history_days(conn, zone)
    finally:
        conn.close()
    return HistoryDaysOut(
        rollover_hour=play_log.HISTORY_DAY_ROLLOVER_HOUR,
        days=[HistoryDayOut(date=day.isoformat(), count=count) for day, count in days],
    )


@router.get("/{day}", response_model=HistoryDayPlaysOut)
def get_history_day(
    day: date,
    request: Request,
    tz: str | None = _TZ_QUERY,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> HistoryDayPlaysOut:
    """That day's plays in play order; an empty day answers an empty list, not 404."""
    return _day_plays(request, day, tz)


@router.post("/{day}/playlist", response_model=HistoryPlaylistOut, status_code=status.HTTP_201_CREATED)
def save_history_day_as_playlist(
    day: date,
    request: Request,
    body: HistoryPlaylistIn | None = None,
    tz: str | None = _TZ_QUERY,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
    store: PlaylistStore = Depends(get_playlist_store),  # noqa: B008  # FastAPI DI
) -> HistoryPlaylistOut:
    """Create a normal playlist holding that day's tracks in play order (repeats kept)."""
    played = [play.stable_id for play in _day_plays(request, day, tz).plays]
    if not played:
        raise HTTPException(
            status_code=404,
            detail={"code": "NO_PLAYS", "message": f"no plays logged on {day.isoformat()}"},
        )
    live = _live_stable_ids(request, played)
    items = [sid for sid in played if sid in live]
    skipped = [sid for sid in dict.fromkeys(played) if sid not in live]
    if not items:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "NO_LIVE_TRACKS",
                "message": f"every track played on {day.isoformat()} has left the collection",
            },
        )
    name = body.name if body is not None and body.name is not None else f"History {day.isoformat()}"
    created = store.create_playlist(name)
    row = store.replace_memberships(created.playlist_id, items, expected_etag=created.etag)
    publish("library.changed", {"kind": "playlists", "ids": [row.playlist_id]})
    return HistoryPlaylistOut(playlist_id=row.playlist_id, name=row.name, track_count=len(row.items), skipped=skipped)


class HistoryDayDeleteOut(BaseModel):
    date: str = Field(description="YYYY-MM-DD as requested")
    start: str = Field(description="Canonical UTC bound used (inclusive)")
    end: str = Field(description="Canonical UTC bound used (exclusive)")
    removed: int
    stable_ids: list[str]


@router.delete("/{day}", response_model=HistoryDayDeleteOut)
def delete_history_day(
    day: date,
    request: Request,
    tz: str | None = _TZ_QUERY,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> HistoryDayDeleteOut:
    start, end = play_log.history_day_bounds(day, _zone(tz))
    conn = state_db.open_rw(_state_db_path(request))
    try:
        conn.execute("BEGIN IMMEDIATE")
        deleted = play_log.delete_plays_between(conn, start, end)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if deleted.count > 0:
        publish("library.changed", {"kind": "plays", "ids": list(deleted.stable_ids)})
    return HistoryDayDeleteOut(
        date=day.isoformat(),
        start=start,
        end=end,
        removed=deleted.count,
        stable_ids=list(deleted.stable_ids),
    )


__all__ = [
    "HistoryDayDeleteOut",
    "HistoryDayOut",
    "HistoryDayPlaysOut",
    "HistoryDaysOut",
    "HistoryPlayOut",
    "HistoryPlaylistIn",
    "HistoryPlaylistOut",
    "router",
]
