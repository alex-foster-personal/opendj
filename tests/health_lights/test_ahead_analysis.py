"""The ahead-of-time analysis drain (NATIVE-21): selection, order, yield, coverage.

Regression lines:
  - if the drain runs anything while a deck plays then broken
  - if a lane batch runs while a present unmapped track lacks a strip then broken
  - if beatgrid or key run while loudness or waveform is missing then broken
  - if a bumped track is not selected first then broken
  - if a complete library still enqueues work then broken
  - if a host-wide lane failure is retried per track then broken
  - if coverage miscounts done / missing / failed then broken

[if] the drain runs work while a deck plays or out of lane order [then] fail, [else stop].
"""
from __future__ import annotations

import pytest

from apps.webui.server import ahead_analysis as aa

pytestmark = pytest.mark.requirement("NATIVE-21")


class World:
    """Injected sources: a library in memory, the seams the engine fills."""

    def __init__(self, present: list[str], mapped: set[str] | None = None) -> None:
        self.present = present
        self.mapped = mapped or set()
        self.strips: set[str] = set()
        self.done: dict[str, set[str]] = {lane: set() for lane, _b in aa.LANE_ORDER}
        self.playing = False
        self.lane_runs: list[tuple[str, list[str]]] = []
        self.strip_runs: list[str] = []
        self.lane_error: str | None = None

    def write_strip(self, sid: str) -> None:
        self.strip_runs.append(sid)
        self.strips.add(sid)

    def run_lane(self, lane: str, _backend: str, ids: list[str]) -> dict[str, str]:
        self.lane_runs.append((lane, list(ids)))
        if self.lane_error is not None:
            return dict.fromkeys(ids, self.lane_error)
        self.done[lane].update(ids)
        return {}

    def drain(self) -> aa.AheadDrain:
        return aa.AheadDrain(
            aa.AheadSources(
                present_fn=lambda: list(self.present),
                mapped_fn=lambda ids: self.mapped & set(ids),
                has_strip_fn=lambda sid: sid in self.strips,
                write_strip_fn=self.write_strip,
                done_fn=lambda lane, _backend: set(self.done[lane]),
                run_lane_fn=self.run_lane,
                playing_fn=lambda: self.playing,
            )
        )


def _run_to_green(drain: aa.AheadDrain, limit: int = 200) -> list[str]:
    outcomes = []
    for _ in range(limit):
        outcome = drain.tick()
        outcomes.append(outcome)
        if outcome == "green":
            return outcomes
    raise AssertionError(f"never green: {outcomes[-5:]}")


def test_playing_deck_runs_nothing() -> None:
    """[if] a deck is playing [then] the tick runs no strip and no lane, [else stop]."""
    world = World(["a", "b"])
    world.playing = True
    drain = world.drain()
    assert drain.tick() == "paused_playing"
    assert world.strip_runs == [] and world.lane_runs == []


def test_strips_before_any_lane_and_mapped_skipped() -> None:
    """[if] an unmapped track lacks a strip [then] no lane runs first, [else stop]."""
    world = World(["a", "b", "c", "r"], mapped={"r"})
    drain = world.drain()
    assert drain.tick() == "ran:strip"
    assert drain.tick() == "ran:strip"
    assert sorted(world.strip_runs) == ["a", "b", "c"]
    assert world.lane_runs == []
    assert "r" not in world.strip_runs


def test_cheap_lanes_before_expensive_ones() -> None:
    """[if] loudness or waveform is missing [then] beatgrid and key wait, [else stop]."""
    world = World([f"t{i}" for i in range(10)])
    _run_to_green(world.drain())
    lanes = [lane for lane, _ids in world.lane_runs]
    order = [lane for lane, _b in aa.LANE_ORDER]
    assert lanes == sorted(lanes, key=order.index)
    assert lanes[0] == "loudness" and lanes[-1] == "key"


def test_complete_library_enqueues_nothing() -> None:
    """[if] every lane and strip is current [then] a tick enqueues nothing, [else stop]."""
    world = World(["a", "b"])
    world.strips = {"a", "b"}
    for lane in world.done:
        world.done[lane] = {"a", "b"}
    assert world.drain().tick() == "green"
    assert world.lane_runs == [] and world.strip_runs == []


def test_bumped_track_goes_first() -> None:
    """[if] a track is bumped [then] it is the next strip and lane target, [else stop]."""
    world = World([f"t{i}" for i in range(10)])
    drain = world.drain()
    drain.bump("t9")
    drain.tick()
    assert world.strip_runs[0] == "t9"
    world.strips = set(world.present)
    drain.tick()
    assert world.lane_runs[0][1][0] == "t9"


def test_front_first_keeps_order() -> None:
    """[if] ids are reordered by a bump [then] only the bumped id moves, [else stop]."""
    assert aa.front_first(["a", "b", "c"], ["c"]) == ["c", "a", "b"]
    assert aa.front_first(["a", "b"], ["zz"]) == ["a", "b"]


def test_host_wide_lane_failure_is_named_once() -> None:
    """[if] a whole chunk fails one way [then] the lane is unavailable, once, [else stop]."""
    world = World([f"t{i}" for i in range(8)])
    world.strips = set(world.present)
    world.lane_error = "Requested resampling engine is unavailable"
    drain = world.drain()
    _run_to_green(drain)
    assert [lane for lane, _ids in world.lane_runs] == ["loudness", "waveform", "beatgrid", "key"]
    cov = drain.coverage()
    assert cov["lanes"]["key"]["unavailable"] == "Requested resampling engine is unavailable"


def test_coverage_counts() -> None:
    """[if] coverage is asked [then] done + missing + failed equals total, [else stop]."""
    counts = aa.coverage_counts(["a", "b", "c", "d"], {"a", "zz"}, {"b": "boom", "a": "old"})
    assert counts == {
        "total": 4, "done": 1, "missing": 2, "failed": 1, "failed_reasons": {"boom": 1},
    }


def test_coverage_reports_progress() -> None:
    """[if] strips are written [then] the strip lane's done count rises, [else stop]."""
    world = World(["a", "b", "r"], mapped={"r"})
    drain = world.drain()
    assert drain.coverage()["lanes"]["strip"]["missing"] == 2
    drain.tick()
    strip = drain.coverage()["lanes"]["strip"]
    assert strip["done"] == 2 and strip["total"] == 2


def test_arm_is_a_fail_fast_enum() -> None:
    """[if] the env var is not on or off [then] arming raises, [else stop]."""
    assert aa.arm_from_environ({}) is True
    assert aa.arm_from_environ({aa.AHEAD_ENV: "off"}) is False
    with pytest.raises(ValueError):
        aa.arm_from_environ({aa.AHEAD_ENV: "maybe"})




def test_per_track_failures_do_not_close_the_lane() -> None:
    """[if] tracks in a chunk fail for different reasons [then] the lane stays open, [else stop]."""
    world = World([f"t{i}" for i in range(4)])
    world.strips = set(world.present)

    def run_lane(lane: str, _backend: str, ids: list[str]) -> dict[str, str]:
        world.lane_runs.append((lane, list(ids)))
        return {sid: f"TrackVanished: {sid}" for sid in ids}

    world.run_lane = run_lane  # type: ignore[method-assign]
    drain = world.drain()
    drain.tick()
    cov = drain.coverage()["lanes"]["loudness"]
    assert cov["unavailable"] is None
    assert cov["failed"] == 4 and cov["missing"] == 0


def test_same_host_cause_with_different_files_closes_the_lane() -> None:
    """[if] every track fails one way, filenames aside [then] the lane is named unavailable, [else stop]."""
    world = World([f"t{i}" for i in range(4)])
    world.strips = set(world.present)
    cause = "Requested resampling engine is unavailable"

    def run_lane(lane: str, _backend: str, ids: list[str]) -> dict[str, str]:
        world.lane_runs.append((lane, list(ids)))
        return {sid: f"TrackUnreadable: {sid}.mp3: ffmpeg exited 234 decoding PCM: x ... {cause}" for sid in ids}

    world.run_lane = run_lane  # type: ignore[method-assign]
    drain = world.drain()
    drain.tick()
    unavailable = drain.coverage()["lanes"]["loudness"]["unavailable"]
    assert unavailable == f"TrackUnreadable: ffmpeg exited 234 decoding PCM ({cause})"
    assert aa.reason_kind("TrackVanished: /a/b.mp3 was gone") == "TrackVanished: /a/b.mp3 was gone"
