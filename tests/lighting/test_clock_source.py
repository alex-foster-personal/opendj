"""Lighting clock REST/prefs integration and real runtime event acceptance.

[if] a clock source is selected [then] only its beats and phrases publish, [else stop].

The constant-tempo grids and phrase spans below are synthesized test inputs.
They exercise the real predictor, runtime and EventHub, not captured engine output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.lighting.clock_api import create_router
from apps.feature_flags import load_flags
from apps.lighting.beat_clock import DeckSample
from apps.lighting.contract import LIGHTING_FLAG_ID
from apps.lighting.http import bind_lighting_runtime
from apps.lighting.runtime import DeckFeed, FeedFrame, LightingRuntime
from apps.lighting.sections import LightSection, SectionSpan, TrackMood
from apps.shared.telemetry import MIRROR_RECEIVED_MONOTONIC_KEY
from apps.webui.server.routes import state, ui_prefs

pytestmark = pytest.mark.requirement("LIGHT-12848")

HOST_NS = 1_000_000_000_000
GRID = [{"n": index % 4 + 1, "time_ms": index * 500.0, "bpm": 120.0} for index in range(20)]
SECTIONS = (
    SectionSpan(0, 1000, LightSection.INTRO, TrackMood.MID, "intro"),
    SectionSpan(1000, 2000, LightSection.DROP, TrackMood.HIGH, "drop"),
    SectionSpan(2000, 10000, LightSection.OUTRO, TrackMood.MID, "outro"),
)
CLOCK_URL = "/api/v1/lighting/clock"


def _app(tmp_path: Path, *, flag_on: bool = True) -> tuple[FastAPI, LightingRuntime]:
    flag_dir = tmp_path / "flags"
    flag_dir.mkdir(exist_ok=True)
    (flag_dir / "feature-flags.json").write_text(json.dumps({LIGHTING_FLAG_ID: flag_on}), encoding="utf-8")
    app = FastAPI()
    app.state.data_dir = tmp_path
    app.state.feature_flags = load_flags(flag_dir)
    runtime = LightingRuntime()
    bind_lighting_runtime(app, runtime)
    controller = ui_prefs.lighting_clock_for_app(app)
    runtime.clock_provider = controller.snapshot
    clock_router = create_router(lambda request: ui_prefs.lighting_clock_for_app(request.app))
    for router in (state.router, ui_prefs.router, clock_router):
        app.include_router(router, prefix="/api/v1")
    return app, runtime


def _mirror(client: TestClient, *, master: str | None = "2", loaded: tuple[str, ...] = ("1", "2")) -> None:
    response = client.put(
        "/api/v1/state/ui-mirror",
        json={"master_deck": master, "decks": {deck: {"stable_id": f"track-{deck}"} for deck in loaded}},
    )
    assert response.status_code == 202
    assert response.json()["accepted"] is True


def _frame(position: float, *, playing: bool = True, decks: tuple[str, ...] = ("1", "2")) -> FeedFrame:
    host_ns = HOST_NS + round(position * 1_000_000)
    return FeedFrame(
        host_time_ns=host_ns,
        decks=tuple(
            DeckFeed(DeckSample(deck, position, 1.0, playing, host_ns), grid=GRID, sections=SECTIONS) for deck in decks
        ),
        weights={deck: 0.5 for deck in decks},
    )


def _messages(runtime: LightingRuntime, position: float, **kwargs: Any) -> list[dict[str, Any]]:
    subscriber = runtime.hub.attach()
    try:
        runtime.process(_frame(position, **kwargs))
        messages = []
        while not subscriber.queue.empty():
            message = subscriber.queue.get_nowait()
            assert message is not None
            messages.append(message)
        return messages
    finally:
        runtime.hub.detach(subscriber)


def _clock_events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [message for message in messages if message["type"] in {"beat", "section_change"}]


def _set(client: TestClient, source: str) -> dict[str, Any]:
    response = client.put(CLOCK_URL, json={"clock_source": source})
    assert response.status_code == 200, response.text
    return response.json()


def test_master_and_override_select_real_published_beats(tmp_path: Path) -> None:
    app, runtime = _app(tmp_path)
    with TestClient(app) as client:
        _mirror(client)
        assert client.get(CLOCK_URL).json() == {
            "clock_source": "master",
            "effective_deck": "2",
            "unavailable_reason": None,
        }
        messages = _messages(runtime, 0)
        assert [event["deck"] for event in messages if event["type"] == "beat"] == ["2"]
        assert next(message for message in messages if message["type"] == "deck_weights")["weights"] == {
            "1": 0.5,
            "2": 0.5,
        }
        assert {deck["deck"] for deck in runtime.status()["decks"]} == {"1", "2"}
        assert {deck.deck for deck in runtime.show_decks(_frame(0))} == {"1", "2"}
        assert _set(client, "deck-1")["effective_deck"] == "1"
        assert [event["deck"] for event in _messages(runtime, 500) if event["type"] == "beat"] == ["1"]
        assert _set(client, "master")["effective_deck"] == "2"
        assert [event["deck"] for event in _messages(runtime, 1000) if event["type"] == "beat"] == ["2"]


def test_master_changes_and_phrases_follow_same_live_source(tmp_path: Path) -> None:
    app, runtime = _app(tmp_path)
    with TestClient(app) as client:
        _mirror(client)
        _messages(runtime, 900)
        changes = [event for event in _messages(runtime, 1000) if event["type"] == "section_change"]
        assert [(event["deck"], event["section"]) for event in changes] == [("2", "drop")]
        _mirror(client, master="1")
        assert client.get(CLOCK_URL).json()["effective_deck"] == "1"
        assert [event["deck"] for event in _messages(runtime, 1500) if event["type"] == "beat"] == ["1"]
        assert not [event for event in _messages(runtime, 1500) if event["type"] == "section_change"]
        _messages(runtime, 1900)
        changes = [event for event in _messages(runtime, 2000) if event["type"] == "section_change"]
        assert [(event["deck"], event["section"]) for event in changes] == [("1", "outro")]
        _set(client, "deck-1")
        _mirror(client, master="2")
        assert client.get(CLOCK_URL).json()["effective_deck"] == "1"
        assert [event["deck"] for event in _messages(runtime, 2500) if event["type"] == "beat"] == ["1"]


def test_live_switches_keep_one_subscriber_connected(tmp_path: Path) -> None:
    app, runtime = _app(tmp_path)
    with TestClient(app) as client:
        _mirror(client)
        subscriber = runtime.hub.attach()
        try:
            runtime.process(_frame(0))
            _set(client, "deck-1")
            runtime.process(_frame(500))
            _set(client, "master")
            runtime.process(_frame(1000))
            events = []
            while not subscriber.queue.empty():
                event = subscriber.queue.get_nowait()
                assert event is not None
                events.append(event)
            beats = [event for event in events if event["type"] == "beat"]
            assert [event["deck"] for event in beats] == ["2", "1", "2"]
            assert [event["beat_index"] for event in beats] == [0, 1, 2]
            assert [event["seq"] for event in events] == sorted({event["seq"] for event in events})
            assert runtime.hub.subscriber_count == 1
        finally:
            runtime.hub.detach(subscriber)


@pytest.mark.parametrize(
    ("source", "status", "code"),
    [
        ("deck-3", 409, "lighting_clock_deck_empty"),
        ("deck-9", 400, "lighting_clock_bad_deck"),
        ("deck-0", 400, "lighting_clock_bad_deck"),
        ("deck-01", 400, "lighting_clock_bad_deck"),
        ("garbage", 400, "lighting_clock_bad_deck"),
    ],
)
def test_refused_clock_write_preserves_file_and_active_source(
    tmp_path: Path, source: str, status: int, code: str
) -> None:
    app, runtime = _app(tmp_path)
    with TestClient(app) as client:
        _mirror(client)
        _set(client, "deck-1")
        path = tmp_path / "state" / "ui-prefs.json"
        before = path.read_bytes()
        response = client.put(CLOCK_URL, json={"clock_source": source})
        assert response.status_code == status
        assert response.json()["detail"]["code"] == code
        assert path.read_bytes() == before
        assert client.get(CLOCK_URL).json()["clock_source"] == "deck-1"
        assert [event["deck"] for event in _messages(runtime, 0) if event["type"] == "beat"] == ["1"]


def test_generic_prefs_shares_validation_and_persists_on_reopen(tmp_path: Path) -> None:
    app, _runtime = _app(tmp_path)
    with TestClient(app) as client:
        _mirror(client)
        assert client.get("/api/v1/ui-prefs").json()["lighting"] == {"clock_source": "master"}
        response = client.put("/api/v1/ui-prefs", json={"theme": "light", "lighting": {"clock_source": "deck-1"}})
        assert response.status_code == 200, response.text
        assert client.get(CLOCK_URL).json()["clock_source"] == "deck-1"
        assert _set(client, "master")["clock_source"] == "master"
        assert client.get("/api/v1/ui-prefs").json()["theme"] == "light"
        _set(client, "deck-1")
        response = client.put("/api/v1/ui-prefs", json={"lighting": {"clock_source": "deck-3"}})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "lighting_clock_deck_empty"
        response = client.put("/api/v1/ui-prefs", json={"theme": "dark", "lighting": {}})
        assert response.status_code == 200
        assert response.json()["lighting"]["clock_source"] == "deck-1"
    reopened, runtime = _app(tmp_path)
    with TestClient(reopened) as client:
        _mirror(client)
        assert client.get(CLOCK_URL).json()["clock_source"] == "deck-1"
        assert [event["deck"] for event in _messages(runtime, 0) if event["type"] == "beat"] == ["1"]


def test_unload_stale_and_absent_master_do_not_guess_a_clock(tmp_path: Path) -> None:
    app, runtime = _app(tmp_path)
    with TestClient(app) as client:
        initial = client.get(CLOCK_URL).json()
        assert initial["clock_source"] == "master"
        assert initial["effective_deck"] is None
        assert initial["unavailable_reason"]
        assert not _clock_events(_messages(runtime, 0))
        _mirror(client)
        _set(client, "deck-1")
        _mirror(client, loaded=("2",))
        unavailable = client.get(CLOCK_URL).json()
        assert unavailable["clock_source"] == "deck-1"
        assert unavailable["effective_deck"] is None
        assert unavailable["unavailable_reason"]
        assert not _clock_events(_messages(runtime, 500))
        _mirror(client)
        assert [event["deck"] for event in _messages(runtime, 1000) if event["type"] == "beat"] == ["1"]
        app.state.ui_mirror[MIRROR_RECEIVED_MONOTONIC_KEY] = 0.0
        assert client.get(CLOCK_URL).json()["effective_deck"] is None
        assert not _clock_events(_messages(runtime, 1500))
        response = client.put(CLOCK_URL, json={"clock_source": "deck-2"})
        assert response.status_code == 409
        _set(client, "master")
        _mirror(client, master=None)
        assert client.get(CLOCK_URL).json()["effective_deck"] is None
        assert not _clock_events(_messages(runtime, 2000))


def test_handover_rebases_cursors_without_historical_events_or_duplicates(tmp_path: Path) -> None:
    app, runtime = _app(tmp_path)
    with TestClient(app) as client:
        _mirror(client)
        _set(client, "deck-1")
        assert [event["beat_index"] for event in _messages(runtime, 0) if event["type"] == "beat"] == [0]
        _set(client, "master")
        _messages(runtime, 1500)
        _set(client, "deck-1")
        events = _clock_events(_messages(runtime, 2500))
        assert [(event["deck"], event["beat_index"]) for event in events] == [("1", 5)]
        _set(client, "deck-1")
        assert not _clock_events(_messages(runtime, 2500))
        assert [event["beat_index"] for event in _messages(runtime, 0) if event["type"] == "beat"] == [0]


def test_paused_and_missing_feed_deck_emit_nothing(tmp_path: Path) -> None:
    app, runtime = _app(tmp_path)
    with TestClient(app) as client:
        _mirror(client)
        _set(client, "deck-1")
        assert not _clock_events(_messages(runtime, 0, playing=False))
        assert not _clock_events(_messages(runtime, 0, decks=("2",)))
        assert [event["deck"] for event in _messages(runtime, 500) if event["type"] == "beat"] == ["1"]


@pytest.mark.parametrize("body", [{}, {"clock_source": None}, {"clock_source": 1}])
def test_clock_rejects_missing_or_wrong_type_fields(tmp_path: Path, body: dict[str, Any]) -> None:
    app, _runtime = _app(tmp_path)
    with TestClient(app) as client:
        assert client.put(CLOCK_URL, json=body).status_code == 422
    assert not (tmp_path / "state" / "ui-prefs.json").exists()


def test_disabled_lighting_flag_gates_both_routes(tmp_path: Path) -> None:
    app, _runtime = _app(tmp_path, flag_on=False)
    with TestClient(app) as client:
        for response in (client.get(CLOCK_URL), client.put(CLOCK_URL, json={"clock_source": "master"})):
            assert response.status_code == 409
            assert response.json()["detail"]["flag"] == LIGHTING_FLAG_ID
    assert not (tmp_path / "state" / "ui-prefs.json").exists()


@pytest.mark.parametrize("value", [None, {"clock_source": None}, {"clock_source": 1}])
def test_generic_prefs_rejects_explicit_null_or_wrong_clock_type(tmp_path: Path, value: Any) -> None:
    app, _runtime = _app(tmp_path)
    with TestClient(app) as client:
        response = client.put("/api/v1/ui-prefs", json={"lighting": value})
        assert response.status_code == 422
    assert not (tmp_path / "state" / "ui-prefs.json").exists()


def test_save_failure_keeps_active_source_and_previous_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, runtime = _app(tmp_path)
    with TestClient(app) as client:
        _mirror(client)
        _set(client, "deck-1")
        path = tmp_path / "state" / "ui-prefs.json"
        before = path.read_bytes()

        def fail_write(_path: Path, _text: str) -> None:
            raise OSError("test filesystem write refused")

        # Only the filesystem dependency fails; route, controller and runtime stay real.
        monkeypatch.setattr(ui_prefs, "_write_atomic", fail_write)
        with pytest.raises(OSError, match="test filesystem write refused"):
            client.put(CLOCK_URL, json={"clock_source": "deck-2"})
        assert path.read_bytes() == before
        assert client.get(CLOCK_URL).json()["clock_source"] == "deck-1"
        assert [event["deck"] for event in _messages(runtime, 0) if event["type"] == "beat"] == ["1"]


def test_malformed_saved_clock_is_visible_failure(tmp_path: Path) -> None:
    path = tmp_path / "state" / "ui-prefs.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"lighting": {"clock_source": "deck-9"}}), encoding="utf-8")
    app, _runtime = _app(tmp_path)
    with TestClient(app) as client:
        response = client.get(CLOCK_URL)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "UI_PREFS_INVALID"


def test_rebinding_runtime_keeps_production_clock_provider(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    replacement = bind_lighting_runtime(app, LightingRuntime())
    with TestClient(app) as client:
        _mirror(client)
        assert [event["deck"] for event in _messages(replacement, 0) if event["type"] == "beat"] == ["2"]
