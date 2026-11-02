"""CLOUDSYNC-14: the deck-playing sync gate must not outlive the page that set it.

The performance page publishes its screen document to the engine once a second
(``PUT /api/v1/state/ui-mirror``) and withdraws it on unmount (``DELETE``). A
page that is killed, crashes or loses its window mid-track sends no DELETE, so
its last "playing" document stays on ``app.state.ui_mirror`` for the life of the
engine process. Before this file existed the gate read that document with no
age bound, and the scheduler journaled ``deferred: deck_playing`` every round
until the engine restarted.

Every mirror below is written by the real route, so the stamp under test is the
one the engine really writes. The only seam is the writer's clock read: a test
places the page's last publish in the past instead of sleeping through the bound.

[if] the page last published longer ago than the bound [then] its playing mirror stops deferring sync, [else stop].
[if] a playing mirror is fresh, exactly at the bound, or of unknown age [then] sync still defers, [else stop].
[if] the wall clock steps after the last publish [then] the verdict does not move, [else stop].

-Claude
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.sync_runtime_gates import (
    DEFER_REASON_DECK_PLAYING,
    DEFER_REASON_PRESSURE_SHED,
    any_deck_playing,
    refuse_sync_round,
    session_xruns_elevated,
)
from apps.shared.telemetry import MIRROR_LIVE_MAX_AGE_S, MIRROR_RECEIVED_MONOTONIC_KEY
from apps.sync_hub import client, maintenance
from apps.sync_hub import scheduler as sync_scheduler
from apps.sync_hub import status as sync_status
from apps.webui.server.routes import state as state_routes
from tests.cloudsync.test_cloudsync_ops_routes import ops_client

pytestmark = pytest.mark.requirement("CLOUDSYNC-14")

_MIRROR_URL = "/api/v1/state/ui-mirror"
_PLAYING: dict[str, Any] = {
    "decks": {
        "1": {"playing": True, "audible": True},
        "2": {"playing": False, "audible": False},
    },
    "xrun_sentinel": {"xruns": 0},
}
_CALM = {"available": True}
_ELEVATED = {"available": True, "kernel_memory_pressure_level": 2}


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _put_mirror(
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],
    *,
    monotonic_s: float,
    wall_clock: datetime | None = None,
) -> dict[str, Any]:
    """Publish ``body`` through the real route with the engine's clocks reading these values.

    Returns the document the engine now holds, which is exactly what the
    scheduler's provider reads (``apps/engine_core/app.py``). The assertion is
    the positive control on the seam: a renamed clock read would otherwise
    leave every "old" mirror below freshly stamped.
    """
    with monkeypatch.context() as clocks:
        clocks.setattr(state_routes, "_received_monotonic_s", lambda: monotonic_s)
        if wall_clock is not None:
            clocks.setattr(state_routes, "_received_at", lambda: _iso(wall_clock))
        response = http.put(_MIRROR_URL, json=body)
    assert response.status_code == 202, response.text
    stored = http.app.state.ui_mirror  # type: ignore[attr-defined]
    assert stored[MIRROR_RECEIVED_MONOTONIC_KEY] == monotonic_s
    return dict(stored)


def _published(
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],
    *,
    seconds_ago: float = 0.0,
    monotonic_s: float | None = None,
    wall_clock: datetime | None = None,
) -> dict[str, Any]:
    """The engine's stored mirror after one real publish ``seconds_ago``, then silence."""
    app = FastAPI()
    app.include_router(state_routes.router, prefix="/api/v1")
    stamp = time.monotonic() - seconds_ago if monotonic_s is None else monotonic_s
    with TestClient(app) as http:
        return _put_mirror(http, monkeypatch, body, monotonic_s=stamp, wall_clock=wall_clock)


def test_get_serves_the_gate_verdict_so_the_ui_agrees(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the server gate calls a mirror stale [then] GET says so for the UI gate, [else stop]."""
    app = FastAPI()
    app.include_router(state_routes.router, prefix="/api/v1")
    with TestClient(app) as http:
        _put_mirror(http, monkeypatch, _PLAYING, monotonic_s=time.monotonic() - 60.0)
        assert http.get(_MIRROR_URL).json()["mirror_stale"] is True
        _put_mirror(http, monkeypatch, _PLAYING, monotonic_s=time.monotonic())
        assert http.get(_MIRROR_URL).json()["mirror_stale"] is False


# ----- the defect: a dead page's last document defers forever ----------------


@pytest.mark.parametrize("seconds_ago", [MIRROR_LIVE_MAX_AGE_S + 1.0, 60.0, 3600.0, 86400.0])
def test_playing_mirror_from_a_page_that_stopped_publishing_releases_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds_ago: float
) -> None:
    """[if] the last publish is past the bound [then] the gate stops deferring, [else stop]."""
    mirror = _published(monkeypatch, _PLAYING, seconds_ago=seconds_ago)

    assert mirror["decks"]["1"]["playing"] is True  # control: the document still says playing
    assert any_deck_playing(mirror) is False
    assert refuse_sync_round(tmp_path, mirror, pressure_payload=_CALM) is None


def test_stale_playing_mirror_does_not_shed_for_pressure_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a dead page's mirror meets elevated pressure [then] no pressure_shed deferral, [else stop]."""
    stale = _published(monkeypatch, {**_PLAYING, "xrun_sentinel": {"xruns": 3}}, seconds_ago=60.0)
    fresh = _published(monkeypatch, {**_PLAYING, "xrun_sentinel": {"xruns": 3}})

    assert refuse_sync_round(tmp_path, stale, pressure_payload=_ELEVATED) is None
    assert session_xruns_elevated(stale) is False
    # Control: the same document, fresh, sheds for both reasons.
    assert refuse_sync_round(tmp_path, fresh, pressure_payload=_ELEVATED) == DEFER_REASON_PRESSURE_SHED
    assert session_xruns_elevated(fresh) is True


# ----- the overshoot: a live set must still defer ----------------------------


def test_fresh_playing_mirror_still_defers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a deck is playing on a page that is still publishing [then] sync defers, [else stop]."""
    mirror = _published(monkeypatch, _PLAYING)

    assert any_deck_playing(mirror) is True
    assert refuse_sync_round(tmp_path, mirror, pressure_payload=_CALM) == DEFER_REASON_DECK_PLAYING


def test_the_bound_is_inclusive_and_is_the_telemetry_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a playing mirror is exactly the bound old [then] it still defers and one tick later it does not, [else stop].

    Exactly representable floats on purpose: ``(stamp + 15.0) - stamp`` is not
    15.0 for an arbitrary monotonic reading, and a boundary test that is off by
    one ulp tests nothing.
    """
    mirror = _published(monkeypatch, _PLAYING, monotonic_s=1000.0)
    at_bound = 1000.0 + MIRROR_LIVE_MAX_AGE_S
    past_bound = math.nextafter(at_bound, math.inf)

    assert MIRROR_LIVE_MAX_AGE_S == 15.0
    assert any_deck_playing(mirror, now_monotonic_s=at_bound) is True
    assert any_deck_playing(mirror, now_monotonic_s=past_bound) is False
    assert (
        refuse_sync_round(tmp_path, mirror, pressure_payload=_CALM, now_monotonic_s=at_bound)
        == DEFER_REASON_DECK_PLAYING
    )
    assert refuse_sync_round(tmp_path, mirror, pressure_payload=_CALM, now_monotonic_s=past_bound) is None


@pytest.mark.parametrize(
    "stamp",
    ["absent", None, "1000.0", True, math.nan, math.inf, -math.inf, "ahead-of-this-clock"],
)
def test_playing_mirror_of_unknown_age_still_defers(tmp_path: Path, stamp: Any) -> None:
    """[if] a playing mirror carries no usable arrival stamp [then] sync defers, [else stop].

    Unknown age is not stale. The engine's route always stamps, so a document
    without a usable stamp did not come through it (a CLI-fetched body, whose
    freshness the CLI checks on the wall clock, or a future writer that forgot),
    and refusing a round is the cheap direction to be wrong in: the journal says
    ``deferred: deck_playing`` and Force sync still works.
    """
    mirror: dict[str, Any] = {"decks": {"1": {"playing": True}}}
    if stamp == "ahead-of-this-clock":
        mirror[MIRROR_RECEIVED_MONOTONIC_KEY] = time.monotonic() + 3600.0
    elif stamp != "absent":
        mirror[MIRROR_RECEIVED_MONOTONIC_KEY] = stamp

    assert any_deck_playing(mirror) is True
    assert refuse_sync_round(tmp_path, mirror, pressure_payload=_CALM) == DEFER_REASON_DECK_PLAYING


# ----- the clock: a wall-clock step must not move the verdict ----------------


def test_wall_clock_stepping_back_does_not_revive_a_dead_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the wall clock steps back after the last publish [then] the dead page's mirror still goes stale, [else stop].

    The stored ``received_at`` reads an hour AHEAD of now, which is what a
    one-hour backward step leaves behind. A wall-clock age is then negative,
    and a negative age passes any ``age > bound`` test as fresh for an hour.
    """
    ahead = datetime.now(UTC) + timedelta(hours=1)
    mirror = _published(monkeypatch, _PLAYING, seconds_ago=60.0, wall_clock=ahead)

    assert mirror["received_at"] == _iso(ahead)  # control: the wall stamp really is in the future
    assert refuse_sync_round(tmp_path, mirror, pressure_payload=_CALM) is None


def test_wall_clock_stepping_forward_does_not_release_a_live_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the wall clock steps forward under a publishing page [then] its playing mirror still defers, [else stop]."""
    behind = datetime.now(UTC) - timedelta(hours=1)
    mirror = _published(monkeypatch, _PLAYING, seconds_ago=1.0, wall_clock=behind)

    assert mirror["received_at"] == _iso(behind)  # control: the wall stamp really is an hour old
    assert refuse_sync_round(tmp_path, mirror, pressure_payload=_CALM) == DEFER_REASON_DECK_PLAYING


# ----- end to end: the scheduler and the Sync now route -----------------------


def test_scheduler_syncs_again_once_the_page_is_gone(
    enroll_live_hub: str, enroll_spoke_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the page dies mid-track [then] the scheduler defers while it is fresh and syncs once it is stale, [else stop].

    The real scheduler, reading the mirror the way the engine wires it, running
    the real ``maintenance.sync`` against a real hub. A page that published a
    moment ago defers; the same page, silent past the bound, does not.
    """
    app = FastAPI()
    app.include_router(state_routes.router, prefix="/api/v1")
    calls: list[str] = []

    def recording_sync(data_dir: Path, hub_url: str, name: str | None) -> client.SyncResult:
        calls.append(hub_url)
        return maintenance.sync(data_dir, hub_url, name=name)

    scheduler = sync_scheduler.CloudSyncScheduler(
        enroll_spoke_dir,
        env={},
        sync_fn=recording_sync,
        ui_mirror_provider=lambda: getattr(app.state, "ui_mirror", None),
        pressure_reader=lambda: _CALM,
    )
    with TestClient(app) as http:
        _put_mirror(http, monkeypatch, _PLAYING, monotonic_s=time.monotonic())
        live_outcome = scheduler.run_round(enroll_live_hub, "spoke")
        live_journal = [(row.status, row.message) for row in sync_status.read_results(enroll_spoke_dir)]
        live_calls = list(calls)
        # The state a minute after the page was killed: no DELETE ever arrived, and
        # the engine's last receipt of the same playing document is 60 s behind its clock.
        _put_mirror(http, monkeypatch, _PLAYING, monotonic_s=time.monotonic() - 60.0)
        dead_outcome = scheduler.run_round(enroll_live_hub, "spoke")

    assert (live_outcome, live_calls) == ("deferred", [])
    assert live_journal == [("deferred", "deferred: deck_playing")]
    assert scheduler.deferred_deck_playing == 1
    assert dead_outcome in {"ok", "inconclusive"}
    assert calls == [enroll_live_hub]
    assert sync_status.read_results(enroll_spoke_dir)[0].status in {"ok", "inconclusive"}


def test_sync_now_route_runs_once_the_page_is_gone(
    enroll_live_hub: str, enroll_spoke_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] Sync now is pressed after the page died mid-track [then] it is not refused as deck_playing, [else stop].

    The real webui app: the same process holds the mirror route and the sync
    route, as the engine does. Fresh is refused with the documented 409; the
    same document past the bound is not.
    """
    with ops_client(enroll_spoke_dir) as http:
        http.app.state.machine_pressure = _CALM  # type: ignore[attr-defined]
        _put_mirror(http, monkeypatch, _PLAYING, monotonic_s=time.monotonic())
        live = http.post("/api/v1/cloudsync/sync", json={"hub_url": enroll_live_hub, "name": "spoke"})
        _put_mirror(http, monkeypatch, _PLAYING, monotonic_s=time.monotonic() - 60.0)
        dead = http.post("/api/v1/cloudsync/sync", json={"hub_url": enroll_live_hub, "name": "spoke"})

    assert live.status_code == 409, live.text
    assert live.json()["detail"]["code"] == "CLOUDSYNC_SYNC_DEFERRED"
    assert "deck_playing" in live.json()["detail"]["message"]
    assert dead.status_code == 200, dead.text
