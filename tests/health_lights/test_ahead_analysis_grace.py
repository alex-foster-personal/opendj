"""The ahead drain's boot grace (PERF-DRAIN-02): bounded at 120 s, ended early by the first index.

Regression lines:
  - if the drain ticks inside the boot grace then the launch index competes with it
  - if the drain still waits once 120 s have passed then a headless engine never drains
  - if a served /tracks/index does not end the grace then the launch waits 120 s for nothing
  - if build_for_app does not arm the grace then the packaged engine never gets it

[if] the drain ticks inside its boot grace or past 120 s waits [then] fail, [else stop].
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.webui.server import ahead_analysis as aa
from apps.webui.server.routes import tracks as tracks_routes

pytestmark = pytest.mark.requirement("PERF-DRAIN-02")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _drain(clock: Clock, runs: list[str], grace_s: float = aa.STARTUP_GRACE_S) -> aa.AheadDrain:
    def write_strip(sid: str) -> None:
        runs.append(sid)

    return aa.AheadDrain(
        aa.AheadSources(
            present_fn=lambda: ["a"],
            mapped_fn=lambda _ids: set(),
            has_strip_fn=lambda sid: sid in runs,
            write_strip_fn=write_strip,
            done_fn=lambda _lane, _backend: {"a"},
            run_lane_fn=lambda _lane, _backend, _ids: {},
            playing_fn=lambda: False,
            blank_tags_fn=set,
            refresh_tags_fn=lambda _sid: True,
            declined_fn=lambda _lane, _backend: {},
        ),
        clock=clock,
        startup_grace_s=grace_s,
    )


def test_no_tick_runs_inside_the_boot_grace() -> None:
    """[if] 119.9 s have passed and no index was served [then] the tick runs nothing, [else stop]."""
    clock, runs = Clock(), list[str]()
    drain = _drain(clock, runs)
    clock.now += 119.9
    assert drain.tick() == aa.STARTUP_GRACE_STATE
    assert runs == [] and drain.in_startup_grace()


def test_the_drain_ticks_once_120s_have_passed_with_no_index() -> None:
    """[if] 120 s pass with no index served [then] the drain ticks anyway, [else stop]."""
    clock, runs = Clock(), list[str]()
    drain = _drain(clock, runs)
    clock.now += 120.0
    assert drain.tick() == "ran:strip"
    assert runs == ["a"] and not drain.in_startup_grace()


def test_an_index_served_at_5s_ends_the_grace_early() -> None:
    """[if] the index is served at t=5 s [then] the next tick runs work, [else stop]."""
    clock, runs = Clock(), list[str]()
    drain = _drain(clock, runs)
    clock.now += 5.0
    assert drain.tick() == aa.STARTUP_GRACE_STATE
    drain.index_served()
    assert drain.tick() == "ran:strip" and runs == ["a"]


def test_an_index_after_the_grace_never_restarts_it() -> None:
    """[if] an index is served after the grace ended [then] ticks still run, [else stop]."""
    clock, runs = Clock(), list[str]()
    drain = _drain(clock, runs)
    clock.now += 121.0
    drain.index_served()
    assert drain.tick() == "ran:strip"


def test_build_for_app_arms_the_120s_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] build_for_app passes no 120 s grace [then] fail, [else stop]."""
    seen: dict[str, Any] = {}

    def capture(_sources: aa.AheadSources, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "drain"

    monkeypatch.setattr(aa, "AheadDrain", capture)
    app = SimpleNamespace(state=SimpleNamespace(state_db_path="/nonexistent/state.db"))
    assert aa.build_for_app(app) == "drain"
    assert aa.STARTUP_GRACE_S == 120.0 and seen["startup_grace_s"] == aa.STARTUP_GRACE_S


def test_serving_the_index_ends_the_drain_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] GET /tracks/index answers [then] the drain is told the index was served, [else stop]."""
    clock, runs = Clock(), list[str]()
    drain = _drain(clock, runs)
    backend = SimpleNamespace(
        library_revision=lambda: "r1",
        list_tracks=lambda _f: SimpleNamespace(items=[], next_cursor=None),
    )
    monkeypatch.setattr(tracks_routes, "_listing_items", lambda *_a: [])
    request: Any = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ahead_analysis=drain)))
    response = tracks_routes.get_track_index(request, None, None, backend)
    assert response.status_code == 200
    assert not drain.in_startup_grace()
