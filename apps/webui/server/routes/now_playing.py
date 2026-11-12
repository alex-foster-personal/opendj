"""NOWP now-playing routes for streamers and overlays.

``GET /api/v1/now-playing`` (issue #10336) answers the one track the room hears.

``GET /api/v1/now-playing/tracklist`` (issue #10341) lists tonight's plays from the
play log, oldest first, where tonight starts at the most recent local rollover
(``play_log.night_start``, the same boundary History uses). Hidden tracks read
``ID - ID``; a play whose track left the library keeps its row with null
metadata. The optional ``tz`` query is an IANA zone name; when omitted the
server's local zone is used.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from apps.b2b_session.peer_now_playing import MAX_MIRROR_AGE_S
from apps.now_playing.http import NowPlayingState, answer, build_rows, fold, observe_decks
from apps.shared.state import db as state_db
from apps.shared.state import play_log
from apps.shared.state.tracklist_redaction import hidden_stable_ids
from apps.shared.telemetry import MIRROR_RECEIVED_MONOTONIC_KEY, mirror_age_s

from ..backend import StateBackend
from ..deps import get_read_state
from .track_plays import _state_db_path

MOUNT_UNDER_API_PREFIX = True
router = APIRouter(tags=["now-playing"])

_now_monotonic = time.monotonic


@dataclass
class _Feed:
    lock: threading.Lock
    state: NowPlayingState


def now_playing_feed(app: FastAPI) -> _Feed:
    feed = getattr(app.state, "now_playing_feed", None)
    if feed is None:
        feed = _Feed(lock=threading.Lock(), state=NowPlayingState())
        app.state.now_playing_feed = feed
    return feed


def observe_now_playing(app: FastAPI, mirror: Mapping[str, Any] | None) -> None:
    """Fold one stored ui-mirror document. No-op when stale, duplicate, or missing monotonic stamp."""
    if mirror is None:
        return
    raw_mono = mirror.get(MIRROR_RECEIVED_MONOTONIC_KEY)
    if not isinstance(raw_mono, (int, float)) or isinstance(raw_mono, bool) or not math.isfinite(float(raw_mono)):
        return
    observed_mono = float(raw_mono)
    observed_wall = mirror.get("received_at") if isinstance(mirror.get("received_at"), str) else None
    decks = observe_decks(mirror)
    feed = now_playing_feed(app)
    with feed.lock:
        feed.state = fold(
            feed.state,
            decks,
            observed_mono=observed_mono,
            observed_wall=observed_wall,
        )


@router.get("/now-playing")
def get_now_playing(request: Request) -> dict[str, Any]:
    """The one track the room hears (3 s settle), previous five, stale flag and generation."""
    mirror = getattr(request.app.state, "ui_mirror", None)
    if mirror is None:
        stale = True
    else:
        age = mirror_age_s(mirror, now_monotonic_s=_now_monotonic())
        stale = age is None or age > MAX_MIRROR_AGE_S
        if not stale:
            observe_now_playing(request.app, mirror)
    feed = now_playing_feed(request.app)
    with feed.lock:
        return answer(feed.state, stale=stale)


def _now() -> datetime:
    return datetime.now(UTC)


class TracklistRowOut(BaseModel):
    stable_id: str
    played_at: str = Field(description="Canonical UTC play time")
    artist: str | None
    title: str | None
    key: str | None
    bpm: float | None
    hidden: bool
    in_library: bool
    deck: int | None
    source: str


class TracklistOut(BaseModel):
    night_start: str = Field(description="Canonical UTC start of tonight (inclusive)")
    night_date: str = Field(description="YYYY-MM-DD of the history day")
    tz: str | None
    count: int
    rows: list[TracklistRowOut]


@router.get("/now-playing/tracklist", response_model=TracklistOut)
def get_tracklist(
    request: Request,
    tz: str | None = Query(
        default=None,
        description="IANA timezone for the night boundary; default is server local.",
    ),
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> dict[str, Any]:
    zone = None
    if tz is not None:
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise HTTPException(
                status_code=422,
                detail={"code": "BAD_TZ", "message": f"unknown timezone {tz!r}"},
            ) from error
    now = _now()
    night = play_log.night_of(now, zone)
    start = play_log.night_start(now, zone)
    conn = state_db.open_ro(_state_db_path(request))
    try:
        plays = play_log.plays_since(conn, start)
        ids = [play.stable_id for play in plays]
        hidden = hidden_stable_ids(conn, ids)
    finally:
        conn.close()
    tracks = backend.get_tracks_bulk(ids, live_only=True)
    rows = build_rows(plays, tracks, hidden)
    return {
        "night_start": start,
        "night_date": night.isoformat(),
        "tz": tz,
        "count": len(rows),
        "rows": rows,
    }
