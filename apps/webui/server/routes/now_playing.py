"""NOWP: ``GET /api/v1/now-playing`` for streamers and overlays (issue #10336)."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, FastAPI, Request

from apps.b2b_session.peer_now_playing import MAX_MIRROR_AGE_S
from apps.now_playing.resolve import NowPlayingState, answer, fold, observe_decks
from apps.shared.telemetry import MIRROR_RECEIVED_MONOTONIC_KEY, mirror_age_s

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
