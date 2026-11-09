"""LIGHT-10573: stage patch body and newest-wins frame feed for live mode.

[if] stage_patch_body or FrameFeed newest-wins behaviour disagrees with the live contract [then] fail, [else stop].
"""

from __future__ import annotations

import asyncio

import pytest

from apps.lighting.patch import Patch, PatchedFixture, StagePosition, VenueStore
from apps.lighting.stage_feed import FrameFeed, stage_patch_body

pytestmark = [pytest.mark.requirement("LIGHT-10573")]


def _fixture(fid: str, x: float, y: float, groups: tuple[str, ...] = ()) -> PatchedFixture:
    return PatchedFixture(fid, "test-rgb-par-3", "3ch", 1, 1, groups, StagePosition(x, y))


def _two_fixture_store() -> VenueStore:
    club = Patch(
        "club",
        (
            _fixture("par-left", 0.1, 0.2, ("left",)),
            _fixture("par-right", 0.9, 0.2, ("right",)),
        ),
    )
    home = Patch("home", (_fixture("solo", 0.5, 0.5),))
    return VenueStore("club", (club, home))


def test_stage_patch_body_active_fixtures_in_order() -> None:
    """[if] stage_patch_body omits active patch ids or positions [then] stop."""
    store = _two_fixture_store()
    body = stage_patch_body(store)
    assert body["name"] == "club"
    assert len(body["fixtures"]) == 2
    assert body["fixtures"][0] == {"id": "par-left", "x": 0.1, "y": 0.2, "groups": ["left"]}
    assert body["fixtures"][1]["id"] == "par-right"


def test_stage_patch_body_version_stable_until_layout_changes() -> None:
    """[if] version changes without a layout edit, or stays same after one [then] stop."""
    store = _two_fixture_store()
    v1 = stage_patch_body(store)["version"]
    v2 = stage_patch_body(store)["version"]
    assert v1 == v2
    moved = store.with_patch(
        Patch(
            "club",
            (
                _fixture("par-left", 0.2, 0.2, ("left",)),
                _fixture("par-right", 0.9, 0.2, ("right",)),
            ),
        )
    )
    v3 = stage_patch_body(moved)["version"]
    assert v3 != v1
    other_active = VenueStore("home", moved.patches)
    assert stage_patch_body(other_active)["name"] == "home"
    assert stage_patch_body(other_active)["fixtures"][0]["id"] == "solo"


def test_frame_feed_subscriber_gets_latest_on_join() -> None:
    """[if] a late subscriber misses the published latest [then] stop."""
    feed = FrameFeed()
    frame = {"t_ns": 0, "patch_version": None, "fixtures": []}
    feed.publish(frame)

    async def run() -> list[dict]:
        gen = feed.frames()
        first = await gen.__anext__()
        await gen.aclose()
        return [first]

    got = asyncio.run(run())
    assert got == [frame]
    assert feed.subscriber_count == 0


def test_frame_feed_two_subscribers_both_receive() -> None:
    """[if] only one subscriber receives a publish [then] stop."""
    feed = FrameFeed()
    frame = {"t_ns": 1, "patch_version": "v", "fixtures": []}

    async def run() -> tuple[dict, dict]:
        g1 = feed.frames()
        g2 = feed.frames()
        feed.publish(frame)
        a = await g1.__anext__()
        b = await g2.__anext__()
        await g1.aclose()
        await g2.aclose()
        return a, b

    a, b = asyncio.run(run())
    assert a == b == frame


def test_frame_feed_newest_wins_for_slow_reader() -> None:
    """[if] a slow reader replays every intermediate frame [then] stop."""
    feed = FrameFeed()
    frames = [
        {"t_ns": 0, "patch_version": None, "fixtures": []},
        {"t_ns": 1, "patch_version": None, "fixtures": []},
        {"t_ns": 2, "patch_version": None, "fixtures": []},
    ]
    for f in frames:
        feed.publish(f)

    async def run() -> list[dict]:
        gen = feed.frames()
        first = await gen.__anext__()
        await gen.aclose()
        return [first]

    got = asyncio.run(run())
    assert got == [frames[-1]]
