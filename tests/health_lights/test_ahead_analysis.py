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

import logging
import sqlite3
import subprocess
import threading
import time

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
    cov = drain.refresh_coverage()
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
    assert drain.refresh_coverage()["lanes"]["strip"]["missing"] == 2
    drain.tick()
    strip = drain.refresh_coverage()["lanes"]["strip"]
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
    cov = drain.refresh_coverage()["lanes"]["loudness"]
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
    unavailable = drain.refresh_coverage()["lanes"]["loudness"]["unavailable"]
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
    assert drain.refresh_coverage()["lanes"]["strip"]["failed"] == 1


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
    tags = drain.refresh_coverage()["lanes"]["tags"]
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
    assert drain.refresh_coverage()["lanes"]["tags"]["failed"] == 0


def test_a_declined_key_is_counted_apart_from_done_and_never_rerun() -> None:
    """[if] a key record declined (no_tonal_center) [then] coverage says declined, not done, [else stop]."""
    world = World(["a", "b"])
    world.strips = {"a", "b"}
    for lane in world.done:
        world.done[lane] = {"a", "b"}
    world.declined["key"] = {"b": "no_tonal_center: ambiguous_margin"}
    drain = world.drain()
    assert drain.tick() == "green", "if a declined record is re-run then broken"
    key = drain.refresh_coverage()["lanes"]["key"]
    assert (key["done"], key["declined"], key["missing"]) == (1, 1, 0)
    assert key["declined_reasons"] == {"no_tonal_center: ambiguous_margin": 1}


#-----------------------------------------------------------------------------
# one track never closes a lane (demon-llama, Fri 2 to Mon 5 Oct 2026)
#-----------------------------------------------------------------------------
#: What a queue run that one track aborts reports for EVERY id in its chunk.
RUN_ABORT = (
    "queue run exit 5: beatgrid.beats[545].t is 268.11095, beyond the record's duration_s 268.1"
)


def _one_bad_track_world(bad: str) -> World:
    """A run containing ``bad`` aborts and every id in it gets the same fallback reason."""
    world = World([f"t{i}" for i in range(4)])
    world.strips = set(world.present)

    def run_lane(lane: str, _backend: str, ids: list[str]) -> dict[str, str]:
        world.lane_runs.append((lane, list(ids)))
        if bad in ids:
            return dict.fromkeys(ids, RUN_ABORT)
        world.done[lane].update(ids)
        return {}

    world.run_lane = run_lane  # type: ignore[method-assign]
    return world


def test_one_track_that_aborts_its_run_fails_alone_and_the_lane_stays_open() -> None:
    """[if] one track aborts a chunk's run [then] only it fails, the lane stays open, [else stop]."""
    world = _one_bad_track_world("t2")
    drain = world.drain()
    drain.tick()
    cov = drain.refresh_coverage()["lanes"]["loudness"]
    assert cov["unavailable"] is None, "if one track's abort closes the lane then broken"
    assert cov["failed"] == 1 and cov["done"] == 3, "if siblings of the bad track are not produced then broken"
    assert world.lane_runs[1:] == [("loudness", [sid]) for sid in world.present]


def test_a_shared_reason_every_track_reproduces_alone_closes_the_lane() -> None:
    """[if] every track alone fails one unknown way [then] the lane is unavailable once, [else stop]."""
    world = World([f"t{i}" for i in range(8)])
    world.strips = set(world.present)
    world.lane_error = "BackendNotAvailable: no checkpoint"
    drain = world.drain()
    _run_to_green(drain)
    cov = drain.refresh_coverage()["lanes"]
    assert all(cov[lane]["unavailable"] == world.lane_error for lane, _b in aa.LANE_ORDER)
    # one chunk plus its four solo reruns per lane, then never again
    assert len(world.lane_runs) == len(aa.LANE_ORDER) * (1 + aa.LANE_CHUNK)


def test_solo_reruns_stop_when_a_deck_starts_playing() -> None:
    """[if] a deck starts during the solo reruns [then] no further track runs, [else stop]."""
    world = _one_bad_track_world("t0")

    def run_lane(lane: str, _backend: str, ids: list[str]) -> dict[str, str]:
        world.lane_runs.append((lane, list(ids)))
        world.playing = True
        return dict.fromkeys(ids, RUN_ABORT)

    world.run_lane = run_lane  # type: ignore[method-assign]
    drain = world.drain()
    drain.tick()
    assert world.lane_runs == [("loudness", ["t0", "t1", "t2", "t3"])]
    assert drain.refresh_coverage()["lanes"]["loudness"]["unavailable"] is None


#-----------------------------------------------------------------------------
# a busy tick never wedges coverage or the drain (silver, Mon 5 Oct 2026)
#-----------------------------------------------------------------------------
FAST_BUDGETS = {name: 0.3 for name in aa.PHASE_BUDGETS_S}


def test_coverage_answers_from_the_snapshot_while_a_tick_is_stuck() -> None:
    """[if] a tick is stuck in a source [then] coverage still answers at once, [else stop]."""
    world = World(["a", "b"])
    gate = threading.Event()
    drain = world.drain()
    drain.refresh_coverage()
    drain._src.present_fn = lambda: (gate.wait(5), list(world.present))[1]  # type: ignore[misc]
    ticker = threading.Thread(target=drain.tick)
    ticker.start()
    started = time.monotonic()
    cov = drain.coverage()
    took = time.monotonic() - started
    gate.set()
    ticker.join(5)
    assert took < 0.2, f"if coverage waits on a stuck tick then broken (took {took:.2f} s)"
    assert cov["present"] == 2 and cov["computed_at"] is not None


def test_coverage_before_any_snapshot_says_so() -> None:
    """[if] coverage was never computed [then] it says computed_at None, not zeros, [else stop]."""
    cov = World(["a"]).drain().coverage()
    assert cov["computed_at"] is None and cov["lanes"] == {} and cov["present"] is None


def test_a_phase_past_its_budget_is_named_and_fails_only_its_track(caplog: pytest.LogCaptureFixture) -> None:
    """[if] a tag read hangs [then] that track fails by name and the drain moves on, [else stop]."""
    world = World(["a", "b"])
    world.blank = {"a"}
    gate = threading.Event()
    real = world.refresh_tags

    def hang(sid: str) -> bool:
        gate.wait(5)
        return real(sid)

    world.refresh_tags = hang  # type: ignore[method-assign]
    drain = aa.AheadDrain(world.drain()._src, phase_budgets_s=FAST_BUDGETS)
    with caplog.at_level(logging.WARNING):
        assert drain.tick() == "timeout:tags"
    assert "phase tags exceeded its 0.3 s budget on a" in caplog.text, "if the stuck phase is not named then broken"
    assert drain.tick() == "ran:strip", "if a stuck phase wedges the phases after it then broken"
    reasons = drain.refresh_coverage()["lanes"]["tags"]["failed_reasons"]
    gate.set()
    assert [why for why in reasons if "timed out" in why], "if the hung track is not failed by name then broken"


def test_an_abandoned_phase_is_never_started_twice() -> None:
    """[if] an abandoned phase is still running [then] the tick waits, never a 2nd copy, [else stop]."""
    world = World(["a"])
    gate = threading.Event()
    calls: list[int] = []

    def stuck() -> list[str]:
        calls.append(1)
        gate.wait(5)
        return ["a"]

    src = world.drain()._src
    src.present_fn = stuck
    drain = aa.AheadDrain(src, phase_budgets_s=FAST_BUDGETS)
    assert drain.tick() == "timeout:present"
    assert drain.tick() == "waiting:present"
    assert len(calls) == 1, "if an abandoned phase is started again while running then broken"
    gate.set()
    time.sleep(0.1)
    drain.tick()
    assert len(calls) == 2, "if a returned phase can never run again then broken"


def test_done_ids_are_read_once_per_lane_per_tick() -> None:
    """[if] a tick plans lane work [then] done_fn runs once per lane, not per track, [else stop]."""
    world = World([f"t{i}" for i in range(50)])
    world.strips = set(world.present)
    calls: list[str] = []
    src = world.drain()._src
    src.done_fn = lambda lane, _backend: (calls.append(lane), set(world.done[lane]))[1]
    aa.AheadDrain(src).tick()
    assert len(calls) <= len(aa.LANE_ORDER), f"if done_fn runs per track then broken ({len(calls)} calls)"


def test_a_lane_call_that_times_out_fails_its_tracks_not_the_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a queue call times out [then] each track gets a timeout reason, [else stop]."""
    def boom(args: list[str], db: str) -> tuple[int, str, str]:
        raise subprocess.TimeoutExpired(cmd="queue_cli", timeout=aa.QUEUE_TIMEOUT_S)

    monkeypatch.setattr(aa, "_queue_cli", boom)
    run = aa.run_lane_via_queue("db", lambda: sqlite3.connect(":memory:"))
    errors = run("beatgrid", "own_beatgrid.backfill", ["a", "b"])
    assert errors == dict.fromkeys(["a", "b"], "queue call timed out after 900 s")
