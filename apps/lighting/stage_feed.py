"""Daemon support for ``/lighting`` live mode: active patch body and a newest-wins frame feed."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from apps.lighting.patch import VenueStore
from apps.lighting.stage_replay import parse_frame_message

__all__ = ["FrameFeed", "parse_frame", "parse_frame_message", "stage_patch_body"]


def stage_patch_body(store: VenueStore) -> dict[str, Any]:
    """Shape the active venue patch for the stage visualiser HTTP GET."""
    patch = store.active_patch
    fixtures = [
        {
            "id": fx.id,
            "x": fx.position.x,
            "y": fx.position.y,
            "groups": list(fx.groups),
        }
        for fx in patch.fixtures
    ]
    name = patch.name
    digest = hashlib.sha256(
        json.dumps({"name": name, "fixtures": fixtures}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return {"version": digest, "name": name, "fixtures": fixtures}


def parse_frame(raw: object) -> dict[str, Any]:
    """Validate one frames websocket message (shared with replay)."""
    return parse_frame_message(raw)


@dataclass
class _Subscriber:
    event: asyncio.Event
    seen_seq: int = 0


class FrameFeed:
    """Newest-wins broadcast of lighting frame messages for live visualisation.

    A visualiser only needs the newest fixture state; intermediate frames are
    dropped when a subscriber cannot keep up (no unbounded queue).
    """

    def __init__(self) -> None:
        self._latest: dict[str, Any] | None = None
        self._seq = 0
        self._subscribers: list[_Subscriber] = []

    @property
    def latest(self) -> dict[str, Any] | None:
        return self._latest

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, frame: dict[str, Any]) -> int:
        self._latest = frame
        self._seq += 1
        for sub in self._subscribers:
            sub.event.set()
        return self._seq

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        sub = _Subscriber(asyncio.Event())
        self._subscribers.append(sub)
        try:
            if self._latest is not None:
                sub.seen_seq = self._seq
                yield self._latest
            while True:
                while self._seq > sub.seen_seq:
                    sub.seen_seq = self._seq
                    if self._latest is not None:
                        yield self._latest
                sub.event.clear()
                await sub.event.wait()
        finally:
            if sub in self._subscribers:
                self._subscribers.remove(sub)
