"""Undecodable files are recorded once and skipped (AHEAD-DUD-01).

[if] a file is undecodable [then] it is recorded once per file and skipped, [else stop].

Build 16's verify: the loudness lane sat at 2268/2270 and re-decoded the same two
files (ffmpeg exit 69) every pass. Two duds alone in a chunk also read as one
"shared" reason, so the lane was closed for the whole host.

Regression lines:
  - if an undecodable file is decoded again on the next tick or after a restart then broken
  - if two undecodable files close the lane for every other track then broken
  - if a dud is not retried after "Retry failed analysis" or a change to the file then broken
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apps.webui.server import ahead_analysis as aa
from tests.health_lights.test_ahead_analysis import World

pytestmark = pytest.mark.requirement("NATIVE-21")

DUDS = ("verdad", "eternity")


class DudWorld(World):
    """Two playable-looking files ffmpeg cannot decode; every other track is fine."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(["ok1", "ok2", *DUDS])
        self.strips = set(self.present)
        self.paths = {sid: str(tmp_path / f"{sid}.mp3") for sid in self.present}
        for path in self.paths.values():
            Path(path).write_bytes(b"x")
        self.duds_path = tmp_path / "ahead-analysis-duds.json"
        self.fixed: set[str] = set()

    def run_lane(self, lane: str, _backend: str, ids: list[str]) -> dict[str, str]:
        self.lane_runs.append((lane, list(ids)))
        bad = {sid for sid in ids if self.file_of(sid) in DUDS and self.file_of(sid) not in self.fixed}
        self.done[lane].update(set(ids) - bad)
        return {sid: f"TrackUnreadable: {self.paths[sid]}: ffmpeg exited 69 decoding PCM fingerprint" for sid in bad}

    def drain(self) -> aa.AheadDrain:
        base = super().drain()._src
        sources = aa.AheadSources(**{**vars(base), "paths_fn": lambda: dict(self.paths), "duds_path": self.duds_path})
        return aa.AheadDrain(sources)

    def dud_runs(self) -> int:
        return sum(1 for _lane, ids in self.lane_runs for sid in ids if self.file_of(sid) in DUDS)

    def file_of(self, sid: str) -> str:
        return Path(self.paths[sid]).stem


def _settle(drain: aa.AheadDrain, ticks: int = 12) -> None:
    for _ in range(ticks):
        drain.tick()


def test_a_dud_is_decoded_once_and_never_closes_the_lane(tmp_path: Path) -> None:
    """[if] the last two files fail TrackUnreadable [then] decoded once, lane open, [else stop]."""
    world = DudWorld(tmp_path)
    for lane, _backend in aa.LANE_ORDER:  # build 16's shape: everything else is done
        world.done[lane].update({"ok1", "ok2"})
    drain = world.drain()
    _settle(drain)
    first = world.dud_runs()
    _settle(drain)
    assert world.dud_runs() == first, "an undecodable file was decoded again"
    loudness = drain.refresh_coverage()["lanes"]["loudness"]
    assert loudness["unavailable"] is None
    assert loudness["failed"] == 2 and loudness["missing"] == 0


def test_a_dud_stays_skipped_after_a_restart(tmp_path: Path) -> None:
    """[if] the engine restarts [then] a recorded dud is not decoded again, [else stop]."""
    world = DudWorld(tmp_path)
    _settle(world.drain())
    before = world.dud_runs()
    restarted = world.drain()
    _settle(restarted)
    assert world.dud_runs() == before
    assert restarted.refresh_coverage()["lanes"]["loudness"]["failed"] == 2


def test_a_changed_file_is_tried_again(tmp_path: Path) -> None:
    """[if] a dud's file changes on disk [then] it is decoded again, [else stop]."""
    world = DudWorld(tmp_path)
    drain = world.drain()
    _settle(drain)
    before = world.dud_runs()
    stat = os.stat(world.paths["verdad"])
    os.utime(world.paths["verdad"], ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    world.fixed.add("verdad")
    _settle(drain)
    assert world.dud_runs() > before
    assert "verdad" in world.done["loudness"]


def test_retry_failed_analysis_tries_a_dud_again(tmp_path: Path) -> None:
    """[if] "Retry failed analysis" is pressed [then] a recorded dud is decoded again, [else stop]."""
    world = DudWorld(tmp_path)
    drain = world.drain()
    _settle(drain)
    before = world.dud_runs()
    drain.retry_failed()
    _settle(drain)
    assert world.dud_runs() > before


def test_rows_sharing_one_dud_file_share_one_record(tmp_path: Path) -> None:
    """[if] two rows share one dud file (#5578) [then] both skipped, one record, [else stop]."""
    world = DudWorld(tmp_path)
    world.present.append("verdad-copy")
    world.strips.add("verdad-copy")
    world.paths["verdad-copy"] = world.paths["verdad"]
    for lane, _backend in aa.LANE_ORDER:
        world.done[lane].update({"ok1", "ok2"})
    _settle(world.drain())
    ledger = json.loads(world.duds_path.read_text())
    assert sorted(ledger["loudness"]) == sorted([world.paths["verdad"], world.paths["eternity"]])
    before = world.dud_runs()
    restarted = world.drain()
    _settle(restarted)
    assert world.dud_runs() == before, "a row on a recorded dud file was decoded again"
    assert restarted.refresh_coverage()["lanes"]["loudness"]["failed"] == 3
