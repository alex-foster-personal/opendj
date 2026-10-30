"""AGENT-48: POST /api/v1/dj/transition/plan over a double of the planner (``plan_live`` has its own tests)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.transition_score import SCORER_VERSION, Candidate, Measured
from apps.transition_score.live import DeckNotPlannable, LiveAnswer, LiveRequest, PlanCache
from apps.webui.server.deps import get_library_data_dir, get_library_state_db
from apps.webui.server.routes import dj_transition

URL = "/api/v1/dj/transition/plan"
BODY = {"out_deck": 1, "in_deck": 2, "intent": "energy_up", "recent_styles": ["wave", "melt"]}


def _candidate() -> Candidate:
    return Candidate(
        id="c2",
        plan={"style": "long_blend", "out": {"window": {"start_bar": 97, "bars": 32}}, "rationale": "leave at bar 97"},
        plan_sha256="sha",
        style="long_blend",
        bars=32,
        metrics={},
        factors={},
        vetoes=(),
        measured=Measured(k=5, n=7),
        unknown={},
        score=0.41,
        scorer_version=SCORER_VERSION,
        render_ms=1.0,
        score_ms=1.0,
    )


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    application = FastAPI()
    application.include_router(dj_transition.router, prefix="/api/v1")
    application.dependency_overrides[get_library_state_db] = lambda: tmp_path / "state" / "state.db"
    application.dependency_overrides[get_library_data_dir] = lambda: tmp_path
    application.state.ui_mirror = {"decks": {}}
    return application


def _planner(monkeypatch: pytest.MonkeyPatch, answer: LiveAnswer) -> list[LiveRequest]:
    calls: list[LiveRequest] = []

    def fake(request: LiveRequest, *, mirror: Any, source: Any, cache: Any, saved_for: Any) -> LiveAnswer:
        mirror()  # the route's own reader runs: a closed page must surface as its 409
        calls.append(request)
        return answer

    monkeypatch.setattr(dj_transition, "plan_live", fake)
    return calls


@pytest.mark.requirement("AGENT-48")
def test_an_order_comes_back_with_the_pick(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the planner picks [then] the page gets the order, the style and M, and the request's variety memory arrived, [else stop]."""
    answer = LiveAnswer(
        {"plan": {"id": "plan-1"}}, "intent energy_up", "energy_up", _candidate(), 40.2, 42, 9, 11.0, 22.0, scored=True
    )
    calls = _planner(monkeypatch, answer)
    response = TestClient(app, base_url="http://127.0.0.1:8685").post(URL, json=BODY)
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["order"] == {"plan": {"id": "plan-1"}}
    assert document["pick"]["style"] == "long_blend" and document["pick"]["out_start_bar"] == 97
    assert calls == [LiveRequest(1, 2, "energy_up", ("wave", "melt"), None, False)]


@pytest.mark.requirement("AGENT-48")
def test_no_pick_is_a_200_with_the_reason(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the planner has no pick [then] 200 with a null order and the reason, so the page can fall back, [else stop]."""
    _planner(monkeypatch, LiveAnswer(None, "nothing eligible", "smooth"))
    response = TestClient(app, base_url="http://127.0.0.1:8685").post(URL, json=BODY)
    assert response.status_code == 200
    assert response.json()["order"] is None and response.json()["reason"] == "nothing eligible"


@pytest.mark.requirement("AGENT-48")
def test_refusals_are_named(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the decks match, no page is open, or a deck is empty [then] each refusal has its own code, [else stop]."""
    client = TestClient(app, base_url="http://127.0.0.1:8685")
    assert client.post(URL, json={**BODY, "in_deck": 1}).json()["error"] == "same_deck"
    assert client.post(URL, json={**BODY, "out_deck": 9}).status_code == 422
    app.state.ui_mirror = None
    _planner(monkeypatch, LiveAnswer(None, "unused", "smooth"))
    closed = client.post(URL, json=BODY)
    assert closed.status_code == 409 and closed.json()["error"] == "no_performance_page"

    def empty(*_args: Any, **_kwargs: Any) -> LiveAnswer:
        raise DeckNotPlannable("deck 2 holds no track")

    app.state.ui_mirror = {"decks": {}}
    monkeypatch.setattr(dj_transition, "plan_live", empty)
    refused = client.post(URL, json=BODY)
    assert refused.status_code == 422 and refused.json()["error"] == "deck_empty"


@pytest.mark.requirement("AGENT-50")
def test_a_corrupt_mirror_is_not_reported_as_an_empty_deck(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the planner hits a malformed mirror [then] the failure surfaces as a 500, never as deck_empty, [else stop]."""

    def corrupt(*_args: Any, **_kwargs: Any) -> LiveAnswer:
        raise ValueError("live mirror decks is null, not an object")

    monkeypatch.setattr(dj_transition, "plan_live", corrupt)
    client = TestClient(app, base_url="http://127.0.0.1:8685", raise_server_exceptions=False)
    assert client.post(URL, json=BODY).status_code == 500


@pytest.mark.requirement("AGENT-52")
def test_prepare_runs_once_per_pair_and_reports_progress(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] prepare is called twice for a pair [then] one job runs and the answer says how far it got, [else stop]."""
    app.state.ui_mirror = {"decks": {"1": {"stable_id": "a"}, "2": {"stable_id": "b"}}}
    jobs: list[LiveRequest] = []

    def fake(request: LiveRequest, key: Any, *, mirror: Any, source: Any, cache: PlanCache) -> None:
        jobs.append(request)
        cache.finish(key, "ready")

    monkeypatch.setattr(dj_transition, "prepare_live", fake)
    client = TestClient(app, base_url="http://127.0.0.1:8685")
    body = {"out_deck": 1, "in_deck": 2, "intent": "smooth"}
    assert client.post("/api/v1/dj/transition/prepare", json=body).status_code == 200
    dj_transition._PREPARE_POOL.submit(lambda: None).result()  # the job above has run
    again = client.post("/api/v1/dj/transition/prepare", json=body).json()
    assert again["state"] == "ready" and len(jobs) == 1
    app.state.ui_mirror = {"decks": {"1": {"stable_id": "a"}, "2": {"stable_id": None}}}
    assert client.post("/api/v1/dj/transition/prepare", json=body).json()["error"] == "deck_empty"
