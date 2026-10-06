"""The engine boot grace (PERF-BOOT-01): background scanners wait for the launch's index.

Regression lines:
  - if the coverage drain takes a snapshot inside the boot grace then the launch index competes with it
  - if the reconcile scan or a coverage re-measure starts inside the grace then the same
  - if the path availability refresher flushes inside the grace then the same
  - if a served /tracks/index does not end the grace then the launch waits 60 s for nothing
  - if the grace outlives 60 s with no index then a headless engine never scans
  - if the daemon factory does not arm the grace then the packaged engine never gets it

[if] a scanner runs inside the grace, or waits past 60 s [then] fail, [else stop].
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from apps.webui.server import boot_grace as bg
from apps.webui.server import coverage_cache as cc
from apps.webui.server import coverage_drain as cd
from apps.webui.server import coverage_outcomes as co
from apps.webui.server import path_availability_refresh as par
from apps.webui.server.routes import tracks as tracks_routes

pytestmark = pytest.mark.requirement("PERF-BOOT-01")

APP_PY = Path(bg.__file__).with_name("app.py")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class Measured(Exception):
    """The drain reached its coverage snapshot."""


def _drain(tmp_path: Path, grace: bg.BootGrace) -> tuple[cd.CoverageDrain, list[int]]:
    snapshots: list[int] = []

    def snapshot() -> Any:
        snapshots.append(1)
        raise Measured

    drain = cd.CoverageDrain(
        snapshot_fn=snapshot,
        jobs={},
        playing_fn=lambda: False,
        outcomes=co.OutcomeStore(co.store_path(tmp_path)),
        config=cd.DrainConfig(cd.config_path(tmp_path)),
        boot_grace=grace,
    )
    return drain, snapshots


#-----------------------------------------------------------------------------
# the grace itself
#-----------------------------------------------------------------------------
def test_the_grace_holds_until_60s_then_lets_go() -> None:
    """[if] 59.9 s pass with no index [then] active, and at 60 s not, [else stop]."""
    clock = Clock()
    grace = bg.BootGrace(bg.BOOT_GRACE_S, clock=clock)
    assert bg.BOOT_GRACE_S == 60.0
    clock.now += 59.9
    assert grace.active()
    clock.now += 0.1
    assert not grace.active()
    grace.wait()  # returns at once, it never strands a waiter


def test_ending_the_grace_releases_a_waiter_at_once() -> None:
    """[if] end() is called while a scanner waits [then] the waiter returns, [else stop]."""
    grace = bg.BootGrace(bg.BOOT_GRACE_S)
    released = threading.Event()

    def scanner() -> None:
        grace.wait()
        released.set()

    waiter = threading.Thread(target=scanner, daemon=True)
    waiter.start()
    assert not released.wait(0.2), "the waiter must hold while the grace is active"
    grace.end()
    assert released.wait(2.0)
    assert not grace.active()


def test_a_stopping_scanner_does_not_wait_out_the_grace() -> None:
    """[if] the scanner's stop flag is set [then] wait() returns inside the grace, [else stop]."""
    grace, stop = bg.BootGrace(bg.BOOT_GRACE_S), threading.Event()
    stop.set()
    started = time.monotonic()
    grace.wait(stop)
    assert time.monotonic() - started < 1.0 and grace.active()


def test_an_app_the_daemon_did_not_arm_has_no_grace() -> None:
    """[if] no grace was armed [then] for_app() is never active, [else stop]."""
    app = SimpleNamespace(state=SimpleNamespace())
    assert not bg.for_app(app).active()
    assert bg.arm(app) is app.state.boot_grace and bg.for_app(app).active()


def test_the_daemon_factory_arms_the_grace_before_the_startup_scan() -> None:
    """[if] the daemon arms the grace after the startup scan [then] fail, [else stop]."""
    source = APP_PY.read_text(encoding="utf-8")
    assert source.index("boot_grace.arm(app)") < source.index("reconcile_routes.warm_summary_snapshot(app, backend)")


#-----------------------------------------------------------------------------
# the three scanners
#-----------------------------------------------------------------------------
def test_the_coverage_drain_takes_no_snapshot_inside_the_grace(tmp_path: Path) -> None:
    """[if] the grace holds [then] no drain snapshot; after end() one, [else stop]."""
    clock = Clock()
    grace = bg.BootGrace(bg.BOOT_GRACE_S, clock=clock)
    drain, snapshots = _drain(tmp_path, grace)
    assert drain.tick() == cd.STARTUP_GRACE_STATE
    assert snapshots == []
    grace.end()
    with pytest.raises(Measured):
        drain.tick()
    assert snapshots == [1]


def test_the_coverage_drain_scans_after_60s_with_no_index(tmp_path: Path) -> None:
    """[if] 60 s pass with no index [then] the drain takes its snapshot, [else stop]."""
    clock = Clock()
    drain, snapshots = _drain(tmp_path, bg.BootGrace(bg.BOOT_GRACE_S, clock=clock))
    clock.now += 60.0
    with pytest.raises(Measured):
        drain.tick()
    assert snapshots == [1]


def test_control_a_drain_with_no_grace_scans_on_its_first_tick(tmp_path: Path) -> None:
    """[if] no grace is armed [then] the first tick snapshots (control), [else stop]."""
    drain, snapshots = _drain(tmp_path, bg.NO_GRACE)
    with pytest.raises(Measured):
        drain.tick()
    assert snapshots == [1]


def test_a_background_coverage_measurement_waits_for_the_grace() -> None:
    """[if] a scan starts inside the grace [then] it measures only after it, [else stop]."""
    grace = bg.BootGrace(bg.BOOT_GRACE_S)
    measured = threading.Event()
    cache = cc.CoverageCache(background_gate=grace.wait)

    def compute() -> dict[str, object]:
        measured.set()
        return {"total": 1}

    assert cache.peek(compute) is None
    assert not measured.wait(0.3), "the startup scan ran inside the boot grace"
    grace.end()
    assert measured.wait(2.0)


def test_control_a_background_measurement_with_no_gate_runs_at_once() -> None:
    """[if] a cache has no gate [then] it measures at once (control), [else stop]."""
    measured = threading.Event()
    cache = cc.CoverageCache()

    def compute() -> dict[str, object]:
        measured.set()
        return {"total": 1}

    cache.peek(compute)
    assert measured.wait(2.0)


def test_the_path_refresher_flushes_nothing_inside_the_grace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] paths queue inside the grace [then] none is statted till it ends, [else stop]."""
    grace = bg.BootGrace(bg.BOOT_GRACE_S)
    flushed: list[str] = []
    refresher = par.PathAvailabilityRefresher(data_dir=tmp_path, state_db_path=tmp_path / "state.db", grace=grace)
    monkeypatch.setattr(refresher, "_flush", flushed.extend)
    monkeypatch.setattr(refresher, "_persist_unpersisted", lambda: None)
    refresher.start()
    try:
        refresher.schedule(["/music/a.mp3"])
        time.sleep(1.2)  # past the refresher's own 0.5 s tick, twice
        assert flushed == []
        grace.end()
        deadline = time.monotonic() + 3.0
        while not flushed and time.monotonic() < deadline:
            time.sleep(0.05)
        assert flushed == ["/music/a.mp3"]
    finally:
        refresher.stop()


#-----------------------------------------------------------------------------
# the index ends it
#-----------------------------------------------------------------------------
def test_serving_the_index_ends_the_boot_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] GET /tracks/index answers [then] the engine boot grace is over, [else stop]."""
    grace = bg.BootGrace(bg.BOOT_GRACE_S)
    backend = SimpleNamespace(
        library_revision=lambda: "r1",
        list_tracks=lambda _f: SimpleNamespace(items=[], next_cursor=None),
    )
    monkeypatch.setattr(tracks_routes, "_listing_items", lambda *_a: [])
    request: Any = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(boot_grace=grace)))
    response = tracks_routes.get_track_index(request, None, None, backend)
    assert response.status_code == 200
    assert not grace.active()
