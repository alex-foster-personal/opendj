"""MEMORY-02, MEMORY-03, MEMORY-06: the transition memory routes, and capture end to end through the mirror route.

[if] a plan is PUT [then] GET returns it with its meta, DELETE clears it, a miss is 404, [else stop].
[if] a document is not a plan [then] 422 and nothing is stored, [else stop].
[if] a hand mix publishes through PUT /state/ui-mirror [then] the pair's GET holds a captured plan, [else stop].
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.autodj.capture import TransitionCapture
from apps.autodj.plan import parse_plan, validate_plan
from apps.autodj.profile import TrackProfile
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.webui.server.routes import pair_transition, state
from tests.autodj.test_capture import _deck, _mirror
from tests.autodj.test_capture import profile as _profile
from tests.autodj.test_pair_memory import plan_document

pytestmark = pytest.mark.requirement("MEMORY-02")

PATH = "/api/v1/autodj/pair-transition"
PAIR = {"out": "out-track", "in": "in-track"}
#: A monotonic published_at across a test's mixes, so no publish is refused as stale.
_TICK = [0]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    state_schema.apply_migrations(conn)
    conn.close()
    app = FastAPI()
    app.state.state_db_path = path
    app.state.pair_capture_profile_source = _profile
    app.include_router(pair_transition.router, prefix="/api/v1")
    app.include_router(state.router, prefix="/api/v1")
    yield TestClient(app)


def _publish(client: TestClient, mirror: dict[str, Any], tick: list[int]) -> None:
    tick[0] += 1
    body = {
        **mirror,
        "client_id": "page",
        "mirror_schema": 2,
        "autoplay_armed": False,
        "autoplay_disarm_reason": "disabled",
        "published_at": f"2026-10-30T12:{tick[0] // 60:02d}:{tick[0] % 60:02d}+00:00",
    }
    response = client.put("/api/v1/state/ui-mirror", json=body)
    assert response.status_code == 202, response.text


def _hand_crossfade(client: TestClient, *, fade_halves: int = 32) -> None:
    tick = _TICK
    _publish(client, _mirror(_deck("out-track", 64.0), _deck("in-track", 1.0, playing=False)), tick)
    for half in range(fade_halves + 1):
        fade = half / fade_halves
        _publish(
            client,
            _mirror(
                _deck("out-track", 65 + half / 2),
                _deck("in-track", 1 + half / 2),
                out_fader=1 - fade,
                in_fader=fade,
            ),
            tick,
        )
    _publish(
        client,
        _mirror(
            _deck("out-track", 65.5 + fade_halves / 2, playing=False),
            _deck("in-track", 1.5 + fade_halves / 2),
            out_fader=0.0,
            in_fader=1.0,
        ),
        tick,
    )


def _settled(client: TestClient) -> dict[str, Any]:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        last = client.get(f"{PATH}/capture").json()["last"]
        if last is not None and last["status"] != "closed":
            return last
        time.sleep(0.01)
    raise AssertionError(f"the capture never settled: {last}")


def test_round_trip_and_clear(client: TestClient) -> None:
    assert client.get(PATH, params=PAIR).status_code == 404
    put = client.put(PATH, json={"plan": plan_document(), "source": "edited", "style": "crossfade"})
    assert put.status_code == 200, put.text
    got = client.get(PATH, params=PAIR).json()
    assert got == put.json()
    assert got["meta"]["source"] == "edited"
    assert got["meta"]["times_played"] == 0
    assert parse_plan(got["plan"]).in_.track == "in-track"
    assert client.delete(PATH, params=PAIR).status_code == 204
    assert client.delete(PATH, params=PAIR).json()["error"] == "pair_transition_not_found"


def test_a_saved_plan_can_be_asked_for_on_the_decks_loaded_now(client: TestClient) -> None:
    client.put(PATH, json={"plan": plan_document(), "source": "planner", "style": "crossfade"})
    moved = client.get(PATH, params={**PAIR, "out_deck": 2, "in_deck": 1}).json()
    assert (moved["plan"]["out"]["deck"], moved["plan"]["in"]["deck"]) == (2, 1)
    lone = client.get(PATH, params={**PAIR, "out_deck": 2})
    assert (lone.status_code, lone.json()["error"]) == (422, "plan_invalid")


def test_a_non_plan_is_refused_and_not_stored(client: TestClient) -> None:
    response = client.put(PATH, json={"plan": {"style": "crossfade"}, "source": "edited", "style": "crossfade"})
    assert (response.status_code, response.json()["error"]) == (422, "plan_invalid")
    assert client.get(PATH, params=PAIR).status_code == 404


def test_an_unknown_source_is_a_validation_error(client: TestClient) -> None:
    response = client.put(PATH, json={"plan": plan_document(), "source": "guessed", "style": "crossfade"})
    assert response.status_code == 422


@pytest.mark.requirement("MEMORY-03")
def test_played_saves_the_plan_that_ran_then_counts_the_replay(client: TestClient) -> None:
    assert client.post(f"{PATH}/played", json=PAIR).status_code == 404
    first = client.post(
        f"{PATH}/played", json={**PAIR, "plan": plan_document(), "source": "planner", "style": "crossfade"}
    )
    assert first.status_code == 200, first.text
    assert first.json()["meta"]["times_played"] == 1
    assert client.post(f"{PATH}/played", json=PAIR).json()["meta"]["times_played"] == 2
    assert client.get(PATH, params=PAIR).json()["meta"]["times_played"] == 2


@pytest.mark.requirement("MEMORY-03")
def test_played_with_a_plan_for_another_pair_is_refused(client: TestClient) -> None:
    body = {"out": "someone", "in": "else", "plan": plan_document(), "source": "planner", "style": "crossfade"}
    response = client.post(f"{PATH}/played", json=body)
    assert (response.status_code, response.json()["error"]) == (422, "plan_invalid")


@pytest.mark.requirement("MEMORY-06")
def test_a_hand_mix_through_the_mirror_route_is_the_pairs_default(client: TestClient) -> None:
    _hand_crossfade(client)
    assert _settled(client)["status"] == "saved"
    saved = client.get(PATH, params=PAIR).json()
    assert saved["meta"]["source"] == "captured"
    assert saved["meta"]["times_played"] == 1
    plan = parse_plan(saved["plan"])
    assert (plan.out.start_bar, plan.in_.start_bar, plan.bars) == (65, 1, 16)
    assert validate_plan(plan, _profile("out-track"), _profile("in-track")).ok


@pytest.mark.requirement("MEMORY-06")
def test_a_captured_plan_that_cannot_replay_is_refused_and_says_why(client: TestClient) -> None:
    """The outgoing window runs past the track's last downbeat: validate_plan refuses it, so nothing is saved."""
    client.app.state.pair_capture_profile_source = lambda stable_id: _profile(stable_id, bars=70)
    _hand_crossfade(client)
    last = _settled(client)
    assert last["status"] == "refused"
    assert "last downbeat" in last["reason"]
    assert client.get(PATH, params=PAIR).status_code == 404


@pytest.mark.requirement("MEMORY-06")
def test_a_broken_mirror_still_publishes_and_the_fault_is_shown(client: TestClient) -> None:
    tick = [0]
    _publish(client, _mirror(_deck("a", 10.0), _deck("b", 1.0, playing=False)), tick)
    broken = _mirror(_deck("a", 11.0), _deck("b", 1.0))
    broken["mixer"]["channels"]["2"]["fader"] = "loud"
    _publish(client, broken, tick)
    last = client.get(f"{PATH}/capture").json()["last"]
    assert last["status"] == "failed"
    assert "fader" in last["reason"]
    assert isinstance(client.app.state.pair_capture, TransitionCapture)


def test_a_mirror_deck_that_is_not_an_object_still_publishes_and_the_fault_is_shown(client: TestClient) -> None:
    tick = [0]
    broken = _mirror(_deck("a", 11.0), _deck("b", 1.0))
    broken["decks"]["2"] = "playing"
    _publish(client, broken, tick)
    last = client.get(f"{PATH}/capture").json()["last"]
    assert last["status"] == "failed"
    assert "live mirror deck" in last["reason"]


@pytest.mark.requirement("MEMORY-06")
def test_saves_land_in_the_order_their_sessions_closed(client: TestClient) -> None:
    """The first save stalls on its profile: the second session's plan must still be the one left standing."""
    release = threading.Event()
    calls: list[str] = []

    def slow_first(stable_id: str) -> TrackProfile:
        calls.append(stable_id)
        if len(calls) == 1:
            release.wait(5.0)
        return _profile(stable_id)

    client.app.state.pair_capture_profile_source = slow_first
    _hand_crossfade(client)
    _hand_crossfade(client, fade_halves=16)
    overtake_window = time.monotonic() + 0.5  # a second worker would save the newer mix inside this
    while client.get(PATH, params=PAIR).status_code == 404 and time.monotonic() < overtake_window:
        time.sleep(0.01)
    release.set()
    deadline = time.monotonic() + 5.0
    while client.get(PATH, params=PAIR).status_code == 404 or len(calls) < 4:
        assert time.monotonic() < deadline, calls
        time.sleep(0.01)
    client.app.state.pair_capture_saver.shutdown(wait=True)
    plan = parse_plan(client.get(PATH, params=PAIR).json()["plan"])
    assert plan.bars == 8
