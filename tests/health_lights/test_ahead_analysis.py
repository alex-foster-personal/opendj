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

import sqlite3

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
        self.blank: set[str] = set()
        self.unreadable: set[str] = set()
        self.locked: set[str] = set()
        self.declined: dict[str, dict[str, str]] = {lane: {} for lane, _b in aa.LANE_ORDER}
        self.tag_runs: list[str] = []

    def refresh_tags(self, sid: str) -> bool:
        self.tag_runs.append(sid)
        if sid in self.locked:
            self.locked.discard(sid)
            raise sqlite3.OperationalError("database is locked")
        if sid in self.unreadable:
            return False
        self.blank.discard(sid)
        return True

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
                blank_tags_fn=lambda: set(self.blank),
                refresh_tags_fn=self.refresh_tags,
                declined_fn=lambda lane, _backend: dict(self.declined[lane]),
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
    assert unavailable == f"TrackUnreadable: {cause}"
    assert aa.reason_kind("TrackVanished: /a/b.mp3 was gone") == "TrackVanished: /a/b.mp3 was gone"


def test_failed_run_names_its_exception_line() -> None:
    """[if] a queue run dies with a traceback [then] the reason is its exception line, [else stop]."""
    text = "Traceback\n  File x\nBackendNotAvailable: no checkpoint\n  hint: set MDT_FFMPEG at one."
    assert aa._last_line(text) == "BackendNotAvailable: no checkpoint"


def test_failed_run_unwraps_the_cli_error_message() -> None:
    """[if] the queue CLI prints a wrapped [ERROR] [then] the reason is that message, [else stop]."""
    text = "[ERROR] backend 'x' is not available\non this host: BackendNotAvailable: no\nsoxr"
    assert aa._last_line(text) == "backend 'x' is not available on this host: BackendNotAvailable: no soxr"


def test_writer_that_leaves_no_strip_is_not_looped() -> None:
    """[if] a strip write succeeds but no strip appears [then] it is failed once, [else stop]."""
    world = World(["a"])
    world.write_strip = world.strip_runs.append  # type: ignore[method-assign,assignment]
    drain = world.drain()
    drain.tick()
    drain.tick()
    assert world.strip_runs == ["a"]
    assert drain.coverage()["lanes"]["strip"]["failed"] == 1


def test_never_read_tags_are_refreshed_before_any_strip() -> None:
    """[if] a present row was never tag-read [then] it is re-read before strips, [else stop]."""
    world = World(["a", "b", "c"])
    world.blank = {"b", "gone"}
    drain = world.drain()
    assert drain.tick() == "ran:tags"
    assert world.tag_runs == ["b"], "if an absent row or a read row is re-read then broken"
    assert world.strip_runs == [], "if strips run before never-read tags then broken"
    assert drain.tick() == "ran:strip"


def test_a_still_unreadable_file_is_tried_once_not_looped() -> None:
    world = World(["a"])
    world.blank = {"a"}
    world.unreadable = {"a"}
    drain = world.drain()
    _run_to_green(drain)
    assert world.tag_runs == ["a"], "if an unreadable file is re-read every tick then broken"
    tags = drain.coverage()["lanes"]["tags"]
    assert tags["failed"] == 1 and tags["failed_reasons"] == {"the file still reads no tags or no duration": 1}


def test_a_busy_state_db_defers_the_tag_read_instead_of_failing_it() -> None:
    """Live on demon-llama: one boot-time 'database is locked' was recorded as
    that track's verdict and never retried."""
    world = World(["a"])
    world.blank = {"a"}
    world.locked = {"a"}
    drain = world.drain()
    _run_to_green(drain)
    assert world.tag_runs == ["a", "a"], "if a locked write is not retried then broken"
    assert drain.coverage()["lanes"]["tags"]["failed"] == 0


def test_a_declined_key_is_counted_apart_from_done_and_never_rerun() -> None:
    """[if] a key record declined (no_tonal_center) [then] coverage says declined, not done, [else stop]."""
    world = World(["a", "b"])
    world.strips = {"a", "b"}
    for lane in world.done:
        world.done[lane] = {"a", "b"}
    world.declined["key"] = {"b": "no_tonal_center: ambiguous_margin"}
    drain = world.drain()
    assert drain.tick() == "green", "if a declined record is re-run then broken"
    key = drain.coverage()["lanes"]["key"]
    assert (key["done"], key["declined"], key["missing"]) == (1, 1, 0)
    assert key["declined_reasons"] == {"no_tonal_center: ambiguous_margin": 1}
