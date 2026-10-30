"""AUTOPLAN-03, AUTOPLAN-06: the decide route over real profiles and the real chooser; only the track source is a double."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.transition_score.live import PrepareKey
from apps.transition_score.tryer import TryError
from apps.webui.server.deps import get_library_data_dir, get_library_state_db
from apps.webui.server.routes import dj_transition
from tests.autodj.fixtures import profile

OUT, IN = profile("trk-out"), profile("trk-in", key="9A")
DECIDE = "/dj/transition/decide"


class _Source:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.tracks = {OUT.stable_id: OUT, IN.stable_id: IN}

    def profile(self, stable_id: str) -> Any:
        if stable_id not in self.tracks:
            raise TryError(f"{stable_id} has no analysis")
        return self.tracks[stable_id]


def _mirror(bar: int = 40) -> dict[str, Any]:
    channel = {"fader": 1.0, "trim": 0.5, "eq_low": 0.5, "eq_mid": 0.5, "eq_high": 0.5, "filter": 0.5, "assign": "thru"}
    deck = {
        "playing": False,
        "bpm": 128.0,
        "effective_bpm": 128.0,
        "pitch": 1.0,
        "pitch_range": 8.0,
        "quantized_launch_armed": None,
        "sync": {"mode": "off", "enabled": False},
    }
    return {
        "master_deck": 1,
        "autoplay_decision": {"phase": "scheduled", "track": "trk-in"},
        "decks": {
            "1": {**deck, "stable_id": "trk-out", "playing": True, "position": {"ms": OUT.time_of_bar(bar) * 1000.0}},
            "2": {**deck, "stable_id": None, "position": {"ms": 0.0}},
        },
        "mixer": {"crossfader": 0.5, "channels": {"1": dict(channel), "2": {**channel, "fader": 0.0}}},
    }


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setattr(dj_transition, "ServingTrackSource", _Source)
    monkeypatch.setattr(dj_transition, "_serving_origin", lambda *_args: "http://engine.test")
    served = FastAPI()
    served.include_router(dj_transition.router)
    served.dependency_overrides[get_library_state_db] = lambda: tmp_path / "state.db"
    served.dependency_overrides[get_library_data_dir] = lambda: tmp_path
    served.state.ui_mirror = _mirror()
    yield served


def _body(**fields: Any) -> dict[str, Any]:
    at_ms = OUT.time_of_bar(50) * 1000.0
    return {"out_deck": 1, "in_deck": 2, "shortlist": ["trk-in"], "earliest_ms": at_ms, "target_ms": at_ms, **fields}


@pytest.mark.requirement("AUTOPLAN-03")
def test_decide_answers_a_window_ahead_and_a_track(app: FastAPI) -> None:
    """[if] a shortlist can be planned [then] the answer is a track and a window ahead, with M unmeasured, [else stop]."""
    answer = TestClient(app).post(DECIDE, json=_body()).json()
    assert answer["track"] == "trk-in", f"if a plannable shortlist gets no track then broken: {answer['reason']}"
    assert answer["window"]["start_bar"] > 50, "if the window is behind the earliest point then broken"
    assert answer["window"]["start_ms"] == round(OUT.time_of_bar(answer["window"]["start_bar"]) * 1000.0, 1)
    assert answer["m_measured"] is False, "if an unrendered decision claims a measured M then broken"


@pytest.mark.requirement("AUTOPLAN-03")
def test_an_unanalyzed_track_is_rejected_by_name_and_the_rest_still_decide(app: FastAPI) -> None:
    """[if] one shortlisted track has no analysis [then] it is rejected by name and the rest decide, [else stop]."""
    answer = TestClient(app).post(DECIDE, json=_body(shortlist=["trk-missing", "trk-in"])).json()
    assert answer["track"] == "trk-in"
    assert answer["rejected"] == [{"track": "trk-missing", "reason": "trk-missing has no analysis"}]


@pytest.mark.requirement("AUTOPLAN-03")
def test_a_shortlist_with_no_plan_is_an_answer_with_a_reason(app: FastAPI) -> None:
    """[if] no shortlisted track has a plan [then] the answer is 200 with a reason, never an error, [else stop]."""
    response = TestClient(app).post(DECIDE, json=_body(shortlist=["trk-missing"]))
    assert response.status_code == 200, "if no pick is an error rather than an answer then broken"
    assert response.json()["track"] is None and response.json()["reason"]


@pytest.mark.requirement("AUTOPLAN-03")
def test_decide_refuses_without_a_page_and_with_one_deck(app: FastAPI) -> None:
    """[if] the decks are the same or no page is open [then] decide refuses by code, [else stop]."""
    assert TestClient(app).post(DECIDE, json=_body(in_deck=1)).json()["error"] == "same_deck"
    app.state.ui_mirror = None
    response = TestClient(app).post(DECIDE, json=_body())
    assert (response.status_code, response.json()["error"]) == (409, "no_performance_page")


@pytest.mark.requirement("AUTOPLAN-06")
def test_autoplay_decision_reads_back_what_the_page_published(app: FastAPI) -> None:
    """[if] the page published a decision [then] the HTTP twin reads it back unchanged, [else stop]."""
    answer = TestClient(app).get("/dj/transition/autoplay-decision").json()
    assert answer == {"autoplay_decision": {"phase": "scheduled", "track": "trk-in"}}


@pytest.mark.requirement("AUTOPLAN-04")
def test_a_status_read_does_not_restart_a_failed_prepare(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a status read finds a failed prepare [then] it says failed and starts nothing, not a warming retry, [else stop]."""
    app.state.ui_mirror = {**_mirror(), "decks": {**_mirror()["decks"], "2": {**_mirror()["decks"]["2"], "stable_id": "trk-in"}}}
    started: list[str] = []

    class _Pool:
        def submit(self, *_args: Any, **_kwargs: Any) -> Any:
            started.append("prepare")
            return Future()

    monkeypatch.setattr(dj_transition, "_PREPARE_POOL", _Pool())
    body = {"out_deck": 1, "in_deck": 2, "intent": "smooth"}
    client = TestClient(app)
    assert client.post("/dj/transition/prepare", json={**body, "status_only": True}).json()["state"] == "warming"
    assert started == ["prepare"], "if a first read starts nothing then a decision waits on no prepare, broken"
    key = PrepareKey("trk-out", "trk-in", 1, 2, "smooth")
    app.state.transition_plan_cache.finish(key, "failed", "no renderer")
    failed = client.post("/dj/transition/prepare", json={**body, "status_only": True}).json()
    assert (failed["state"], failed["reason"]) == ("failed", "no renderer"), "if a failure reads as warming then broken"
    assert started == ["prepare"], "if a status read restarts it then broken"
    assert client.post("/dj/transition/prepare", json=body).json()["state"] == "warming", "control: a plain call still retries"
    assert started == ["prepare", "prepare"]
