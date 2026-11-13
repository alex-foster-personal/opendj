"""LIGHT-05: feed pump, drives ``LightingRuntime`` from ``EngineFeed`` batches.

Each batch becomes one runtime ``FeedFrame`` whose decks come from the injected ``DeckFeedBuilder``
(the caller wires its profile builder; this package never imports the autodj profile builder) and whose
weights are the mixer faders. ``EngineFeedDisconnected`` ends the pump cleanly so the caller can reconnect.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Protocol

from apps.lighting.engine_feed import DeckFeedBuilder, EngineFeedDisconnected, FeedBatch
from apps.lighting.runtime import FeedFrame, LightingRuntime

log = logging.getLogger(__name__)


class BatchSource(Protocol):
    def batches(self) -> AsyncIterator[FeedBatch]: ...


def frame_from_batch(batch: FeedBatch, builder: DeckFeedBuilder) -> FeedFrame:
    """One runtime frame from one engine batch, weighted by the mixer faders."""
    return FeedFrame(batch.received_ns, builder.feeds(batch), dict(batch.mixer.faders))


async def pump(feed: BatchSource, runtime: LightingRuntime, builder: DeckFeedBuilder) -> int:
    """Feed every engine batch to the runtime until the feed disconnects; returns the frame count."""
    count = 0
    try:
        async for batch in feed.batches():
            runtime.process(frame_from_batch(batch, builder))
            count += 1
    except EngineFeedDisconnected as exc:
        log.info("engine feed disconnected after %d frames: %s", count, exc)
    return count


__all__ = ["BatchSource", "frame_from_batch", "pump"]
