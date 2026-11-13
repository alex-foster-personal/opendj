"""LightingRuntime wiring to the OS2L client: 1-based bar on beats and deck profiles reach set_profile.

[if] the runtime has an OS2L client attached [then] beats carry bar and deck profiles reach it, [else stop].
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from apps.lighting.beat_clock import DeckSample
from apps.lighting.contract import ClockSnapshot
from apps.lighting.os2l_client import Os2lClient
from apps.lighting.os2l_codec import Beat, Btn
from apps.lighting.runtime import DeckFeed, FeedFrame, LightingRuntime

pytestmark = pytest.mark.requirement("LIGHT-14126")

BEAT_MS = 500.0
GRID = tuple({"n": n, "time_ms": (n - 1) * BEAT_MS, "bpm": 120.0} for n in range(1, 33))


def _runtime() -> LightingRuntime:
    """A runtime whose lighting clock follows deck 1, the only deck these replays load."""
    return LightingRuntime(lookahead_ms=BEAT_MS, clock_provider=lambda: ClockSnapshot("master", "1", frozenset({"1"})))


@dataclass(frozen=True)
class FakeSection:
    label: str
    start_bar: int
    bars: int


@dataclass(frozen=True)
class FakeProfile:
    bar_energy: tuple[float, ...]
    sections: tuple[FakeSection, ...] = ()


def _replay(runtime: LightingRuntime, profile: FakeProfile | None) -> None:
    for step in range(33):
        position = step * BEAT_MS - 1.0
        sample = DeckSample("1", position, 1.0, True, int(position * 1_000_000) + 10_000_000)
        runtime.process(FeedFrame(sample.host_time_ns, (DeckFeed(sample, GRID, profile=profile),), {"1": 1.0}))


def _queued(client: Os2lClient) -> list:
    return [m for _, m in client._queue._queue]  # type: ignore[attr-defined]


def test_replayed_feed_beats_carry_bar() -> None:
    """[if] the runtime ingests a replayed 8-bar feed [then] beat messages carry bar counting 1..8, [else stop]."""
    runtime = _runtime()
    seen: list[dict] = []
    runtime._message_sinks.append(seen.append)
    _replay(runtime, None)
    bars = [m["bar"] for m in seen if m["type"] == "beat" and m["beat_index"] < len(GRID)]
    assert bars == [b for b in range(1, 9) for _ in range(4)]


def test_deck_profile_reaches_client_and_sends_btn() -> None:
    """[if] a deck loads a track with a profile while OS2L is attached [then] it gets the profile and one btn on, [else stop]."""
    runtime = _runtime()
    client = Os2lClient("127.0.0.1", 1, buttons={"intro": "INTRO", "drop": "DROP"})
    runtime.attach_os2l(client)
    profile = FakeProfile(bar_energy=(0.5,) * 8, sections=(FakeSection("intro", 1, 8),))
    _replay(runtime, profile)
    assert client._profiles["1"] is profile
    btns = [m for m in _queued(client) if isinstance(m, Btn)]
    assert btns == [Btn(name="INTRO", state="on")]


def test_no_profile_default_strength_no_btn() -> None:
    """[if] no profile is known for a deck [then] beats go out with default strength and no btn messages, [else stop]."""
    runtime = _runtime()
    client = Os2lClient("127.0.0.1", 1, buttons={})
    runtime.attach_os2l(client)
    _replay(runtime, None)
    queued = _queued(client)
    assert queued and all(isinstance(m, Beat) and m.strength is None for m in queued)
