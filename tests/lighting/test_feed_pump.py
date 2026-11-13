"""LIGHT-05 feed pump: engine feed batches drive the lighting runtime (issue #14294).

[if] engine feed batches arrive [then] the pump hands each one to the runtime as a FeedFrame, [else stop].
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from apps.lighting.beat_clock import DeckSample
from apps.lighting.engine_feed import (
    DeckFeedBuilder,
    DeckFrame,
    EngineFeedDisconnected,
    FeedBatch,
    MixerSample,
    TrackAnalysis,
)
from apps.lighting.feed_pump import pump
from apps.lighting.runtime import FeedFrame
from apps.lighting.sections import LightSection, SectionSpan, TrackMood

pytestmark = [pytest.mark.asyncio, pytest.mark.requirement("LIGHT-14352")]

SECTIONS = (SectionSpan(0, 4000, LightSection.DROP, TrackMood.HIGH, "drop"),)


def _batch(seq: int, position_ms: float, analysis: TrackAnalysis | None = None) -> FeedBatch:
    sample = DeckSample("1", position_ms, 1.0, True, seq * 1000)
    mixer = MixerSample(0.0, 1.0, 0.5, False, {"1": 1.0}, {"1": "A"})
    return FeedBatch(seq, seq, seq * 1000, (DeckFrame(sample, "t1", analysis, 60000.0, 120.0),), mixer)


class FakeFeed:
    def __init__(self, batches: list[FeedBatch], *, disconnect: bool = False) -> None:
        self._batches = batches
        self._disconnect = disconnect

    async def batches(self) -> AsyncIterator[FeedBatch]:
        for batch in self._batches:
            yield batch
        if self._disconnect:
            raise EngineFeedDisconnected("audio engine closed")


class FakeRuntime:
    def __init__(self) -> None:
        self.frames: list[FeedFrame] = []

    def process(self, frame: FeedFrame) -> Any:
        self.frames.append(frame)


async def test_two_batches_reach_runtime_in_order() -> None:
    """[if] a fake feed yields two batches for deck 1 [then] the runtime receives two DeckFeed updates for deck 1 in order, [else stop]."""
    runtime = FakeRuntime()
    count = await pump(FakeFeed([_batch(1, 100.0), _batch(2, 200.0)]), runtime, DeckFeedBuilder())  # type: ignore[arg-type]
    assert count == 2
    rows = [f.decks[0] for f in runtime.frames]
    assert [r.sample.deck for r in rows] == ["1", "1"]
    assert [r.sample.position_ms for r in rows] == [100.0, 200.0]


async def test_profile_reaches_runtime() -> None:
    """[if] the track analysis has sections [then] the DeckFeed the runtime receives carries that profile, [else stop]."""
    profile = object()
    analysis = TrackAnalysis("t1", (), None, SECTIONS, profile)  # type: ignore[arg-type]
    runtime = FakeRuntime()
    await pump(FakeFeed([_batch(1, 100.0, analysis)]), runtime, DeckFeedBuilder())  # type: ignore[arg-type]
    row = runtime.frames[0].decks[0]
    assert row.profile is profile
    assert tuple(row.sections) == SECTIONS


async def test_disconnect_returns_cleanly() -> None:
    """[if] the feed raises EngineFeedDisconnected [then] pump returns without raising, [else stop]."""
    runtime = FakeRuntime()
    count = await pump(FakeFeed([_batch(1, 100.0)], disconnect=True), runtime, DeckFeedBuilder())  # type: ignore[arg-type]
    assert count == 1
    assert len(runtime.frames) == 1
