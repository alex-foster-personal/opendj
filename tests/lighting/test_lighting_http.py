"""LIGHT-06 HTTP status route and events websocket (issue #9863).

[if] lighting.output is off [then] status is 409 and events refuse, [else stop].
[if] a replayed bar at 120 BPM plays [then] four beats arrive in order, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apps.feature_flags import load_flags
from apps.lighting.beat_clock import DeckSample
from apps.lighting.contract import LIGHTING_FLAG_ID
from apps.lighting.http import bind_lighting_runtime, router
from apps.lighting.runtime import DeckFeed, FeedFrame, LightingRuntime, ReplayFeed
from apps.lighting.sections import LightSection, SectionSpan, TrackMood
from apps.lighting.show_map import list_scene_catalog, load_look_ids
from apps.lighting.show_runtime import DeckPlan, ShowCue

pytestmark = [pytest.mark.requirement("LIGHT-9863")]

HOST_NS = 1_000_000_000_000
GRID = [{"n": i % 4 + 1, "time_ms": i * 500.0, "bpm": 120.0} for i in range(8)]
SECTIONS = (
    SectionSpan(0, 1000, LightSection.INTRO, TrackMood.MID, "intro"),
    SectionSpan(1000, 4000, LightSection.DROP, TrackMood.HIGH, "drop"),
)


def _sample(deck: str, position_ms: float, *, host_ns: int | None = None) -> DeckSample:
    return DeckSample(
        deck=deck,
        position_ms=position_ms,
        rate=1.0,
        playing=True,
        host_time_ns=host_ns if host_ns is not None else HOST_NS + round(position_ms * 1_000_000),
    )


def _bar_frames() -> list[FeedFrame]:
    frames: list[FeedFrame] = []
    for position in range(0, 2000, 100):
        sample = _sample("A", float(position))
        frames.append(
            FeedFrame(
                host_time_ns=sample.host_time_ns,
                decks=(DeckFeed(sample=sample, grid=GRID, sections=SECTIONS),),
                weights={"A": 1.0},
            )
        )
    return frames


def _app(
    tmp_path: Path,
    *,
    flag_on: bool,
    runtime: LightingRuntime | None = None,
) -> tuple[FastAPI, LightingRuntime]:
    flag_dir = tmp_path / "flags"
    flag_dir.mkdir()
    if flag_on:
        (flag_dir / "feature-flags.json").write_text(json.dumps({LIGHTING_FLAG_ID: True}))
    if runtime is None:
        runtime = LightingRuntime()
    app = FastAPI()
    app.state.feature_flags = load_flags(flag_dir)
    bind_lighting_runtime(app, runtime)
    app.include_router(router, prefix="/api/v1")
    return app, runtime


def _drain(ws: Any) -> tuple[list[dict[str, Any]], int | None]:
    messages: list[dict[str, Any]] = []
    code: int | None = None
    try:
        while True:
            messages.append(ws.receive_json())
    except WebSocketDisconnect as exc:
        code = exc.code
    return messages, code


def test_status_is_409_naming_the_flag_when_off(tmp_path: Path) -> None:
    """[if] the flag is off [then] GET status is 409 naming lighting.output."""
    app, runtime = _app(tmp_path, flag_on=False)
    with TestClient(app) as client:
        response = client.get("/api/v1/lighting/status")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["flag"] == "lighting.output"
        assert "lighting.output" in detail["message"]
    assert runtime.has_running_task is False


@pytest.mark.timeout(20)
def test_events_socket_refuses_when_off(tmp_path: Path) -> None:
    """[if] the flag is off [then] the events socket sends refused and closes 1008."""
    app, _runtime = _app(tmp_path, flag_on=False)
    with TestClient(app) as client, client.websocket_connect("/api/v1/lighting/events") as ws:
        first = ws.receive_json()
        assert first["type"] == "refused"
        assert first["status"] == 409
        assert first["detail"]["flag"] == "lighting.output"
        _messages, code = _drain(ws)
    assert code == 1008


def test_status_when_on_without_feed(tmp_path: Path) -> None:
    """[if] the flag is on with no feed [then] status shows empty decks and no feed."""
    app, _runtime = _app(tmp_path, flag_on=True)
    with TestClient(app) as client:
        response = client.get("/api/v1/lighting/status")
        assert response.status_code == 200
        body = response.json()
        assert body["feed_connected"] is False
        assert body["flag"] == "lighting.output"
        assert body["decks"] == []
        assert body["weights"] == {}


@pytest.mark.timeout(20)
def test_one_bar_at_120_bpm_gives_four_ordered_beats(tmp_path: Path) -> None:
    """[if] replay plays one bar at 120 BPM [then] four beats arrive in order."""
    frames = _bar_frames()
    runtime = LightingRuntime()
    runtime.feed = ReplayFeed(frames, start_when=lambda: runtime.hub.wait_for_subscribers(1))
    app, _runtime = _app(tmp_path, flag_on=True, runtime=runtime)
    with TestClient(app) as client, client.websocket_connect("/api/v1/lighting/events") as ws:
        messages, code = _drain(ws)
    beats = [m for m in messages if m["type"] == "beat"]
    assert len(beats) == 4
    assert [b["beat_index"] for b in beats] == [0, 1, 2, 3]
    host_times = [b["at_host_ns"] for b in beats]
    assert host_times == sorted(host_times)
    assert host_times[1] - host_times[0] == 500_000_000
    assert code == 1000


@pytest.mark.timeout(20)
def test_crossing_a_section_boundary_gives_one_section_change(tmp_path: Path) -> None:
    """[if] replay crosses a section boundary [then] one section_change and deck_weights."""
    frames = _bar_frames()
    runtime = LightingRuntime()
    runtime.feed = ReplayFeed(frames, start_when=lambda: runtime.hub.wait_for_subscribers(1))
    app, _ = _app(tmp_path, flag_on=True, runtime=runtime)
    with TestClient(app) as client, client.websocket_connect("/api/v1/lighting/events") as ws:
        messages, _code = _drain(ws)
    changes = [m for m in messages if m["type"] == "section_change"]
    assert len(changes) == 1
    assert changes[0]["previous"] == "intro"
    assert changes[0]["section"] == "drop"
    assert changes[0]["at_host_ns"] == HOST_NS + 1_000_000_000
    weights = [m for m in messages if m["type"] == "deck_weights"]
    assert len(weights) == 1
    assert weights[0]["weights"] == {"A": 1.0}


@pytest.mark.timeout(20)
def test_two_clients_receive_the_same_sequence(tmp_path: Path) -> None:
    """[if] two clients connect [then] both get the same beat sequence."""
    frames = _bar_frames()
    runtime = LightingRuntime()
    runtime.feed = ReplayFeed(frames, start_when=lambda: runtime.hub.wait_for_subscribers(2))
    app, _ = _app(tmp_path, flag_on=True, runtime=runtime)
    with (
        TestClient(app) as client,
        client.websocket_connect("/api/v1/lighting/events") as ws1,
        client.websocket_connect("/api/v1/lighting/events") as ws2,
    ):
        list1, _ = _drain(ws1)
        list2, _ = _drain(ws2)
    assert list1 == list2
    beats = [m for m in list1 if m["type"] == "beat"]
    assert len(beats) == 4


def test_status_reports_bpm_section_and_weights_after_replay() -> None:
    """[if] frames are ingested [then] status shows bpm, section and weights."""
    runtime = LightingRuntime()
    for frame in _bar_frames():
        runtime.ingest(frame)
    status = runtime.status()
    deck_a = next(row for row in status["decks"] if row["deck"] == "A")
    assert deck_a["bpm"] == 120.0
    assert deck_a["section"] == "drop"
    assert status["weights"] == {"A": 1.0}


def test_a_seek_back_does_not_double_beats() -> None:
    """[if] position jumps backward [then] beats do not duplicate within a pass."""
    runtime = LightingRuntime()
    first_pass: list[int] = []
    for frame in _bar_frames()[:10]:
        first_pass.extend(
            message["beat_index"]
            for message in runtime.ingest(frame)
            if message["type"] == "beat"
        )
    second_pass: list[int] = []
    for frame in _bar_frames()[:10]:
        second_pass.extend(
            message["beat_index"]
            for message in runtime.ingest(frame)
            if message["type"] == "beat"
        )
    assert first_pass == second_pass
    assert len(first_pass) == len(set(first_pass))


@pytest.mark.requirement("LIGHT-11778")
def test_scenes_is_409_naming_the_flag_when_off(tmp_path: Path) -> None:
    """[if] flag off [then] GET scenes is 409 naming lighting.output, [else stop]."""
    app, _runtime = _app(tmp_path, flag_on=False)
    with TestClient(app) as client:
        response = client.get("/api/v1/lighting/scenes")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["flag"] == "lighting.output"
        assert "lighting.output" in detail["message"]


@pytest.mark.requirement("LIGHT-11778")
def test_scenes_lists_catalog_ids_when_on(tmp_path: Path) -> None:
    """[if] flag on [then] GET scenes ids match looks_default.json, [else stop]."""
    app, _runtime = _app(tmp_path, flag_on=True)
    expected = [row["id"] for row in list_scene_catalog()]
    with TestClient(app) as client:
        response = client.get("/api/v1/lighting/scenes")
        assert response.status_code == 200
        body = response.json()
        assert [row["id"] for row in body] == expected
        assert load_look_ids() == frozenset(row["id"] for row in body)


@pytest.mark.requirement("LIGHT-11778")
def test_look_is_409_when_flag_off(tmp_path: Path) -> None:
    """[if] flag off [then] GET look is 409, [else stop]."""
    app, _runtime = _app(tmp_path, flag_on=False)
    with TestClient(app) as client:
        response = client.get("/api/v1/lighting/look")
        assert response.status_code == 409


@pytest.mark.requirement("LIGHT-11778")
def test_look_returns_scene_and_phrase_after_feed(tmp_path: Path) -> None:
    """[if] runtime processed a deck frame [then] look has scene_id and phrase_label, [else stop]."""
    sections = (
        SectionSpan(0, 1000, LightSection.INTRO, TrackMood.MID, "intro-phrase"),
        SectionSpan(1000, 4000, LightSection.DROP, TrackMood.HIGH, "drop-phrase"),
    )
    plan = DeckPlan("ta", (ShowCue(0, "wash_warm", 0),), (0, 32))
    sample = _sample("A", 1500.0)
    frame = FeedFrame(
        host_time_ns=sample.host_time_ns,
        decks=(DeckFeed(sample=sample, grid=GRID, sections=sections, plan=plan),),
        weights={"A": 1.0},
    )
    runtime = LightingRuntime()
    runtime.process(frame)
    app, _ = _app(tmp_path, flag_on=True, runtime=runtime)
    with TestClient(app) as client:
        response = client.get("/api/v1/lighting/look")
        assert response.status_code == 200
        body = response.json()
        assert body["scene_id"] == "wash_warm"
        assert body["phrase_label"] == "drop-phrase"
