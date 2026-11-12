"""Tracklist export and HTTP routes (issue #10391)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets import paths as sets_paths_mod
from apps.sets.manifest import AudioSegment, Manifest, write_manifest
from apps.sets.paths import SessionPathError
from apps.sets.soundcloud_export import LICENSING_REMINDER
from apps.sets.state import Event, SetsState
from apps.sets.tracklist_export import (
    build_tracklist,
    format_start_label,
    render_tracklist,
)
from apps.shared.m3u8 import M3u8Entry, render_m3u8
from tests.sets.conftest import mount_sets_router
from tests.sets.test_soundcloud_export import _play, _seed_session

REQ = "SET-12124"


def _seed_sqlite_session(
    sets_root: Path,
    *,
    session_id: str = "s1",
    events: list[Event] | None = None,
    timeline_only_session_start: bool = True,
    duration_ended_at: str = "2026-04-17T23:00:00+00:00",
) -> tuple[SetsState, Path]:
    sess = sets_root / session_id
    sess.mkdir(parents=True, exist_ok=True)
    write_manifest(
        sess,
        Manifest(
            session_id=session_id,
            started_at="2026-04-17T21:30:00+00:00",
            ended_at=duration_ended_at,
            capture_device="test",
            event_count=len(events or []),
            deck_sources=["opendj"],
        ),
    )
    if timeline_only_session_start:
        (sess / "timeline.jsonl").write_text(
            json.dumps(
                _play(
                    session_id,
                    0.0,
                    action="session_start",
                    title=None,
                    artist=None,
                    track_stable_id=None,
                    source="recorder",
                    deck=None,
                )
            )
            + "\n",
            encoding="utf-8",
        )
    state = SetsState(db_path=sets_root / "sets.db")
    state.open_session(session_id, capture_device="test", started_at="2026-04-17T21:30:00+00:00")
    for event in events or []:
        state.record_event(event)
    return state, sess


def _track_event(
    session_id: str,
    timestamp_s: float,
    *,
    stable_id: str,
    title: str | None = None,
    artist: str | None = None,
    deck: str = "A",
    extra: dict | None = None,
) -> Event:
    value = dict(extra or {})
    if title is not None:
        value["title"] = title
    if artist is not None:
        value["artist"] = artist
    return Event(
        session_id=session_id,
        timestamp_s=timestamp_s,
        wall_clock="2026-04-17T21:30:00+00:00",
        action="track_loaded",
        source="opendj",
        deck=deck,
        track_stable_id=stable_id,
        value=value,
    )


@pytest.fixture
def api_test_client(sets_root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path]]:
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", sets_root)
    app = FastAPI()
    mount_sets_router(app)
    with TestClient(app) as client:
        yield client, sets_root


@pytest.mark.requirement(REQ)
def test_rows_from_sqlite_start_labels(sets_root: Path) -> None:
    """[if] sqlite holds plays [then] rows use mm:ss labels in order, [else stop]."""
    events = [
        _track_event("s1", 0.0, stable_id="a", title="One", artist="A1"),
        _track_event("s1", 330.0, stable_id="b", title="Two", artist="A2", deck="B"),
        _track_event("s1", 4325.0, stable_id="c", title="Three", artist="A3"),
    ]
    state, _ = _seed_sqlite_session(sets_root, events=events)
    tl = build_tracklist("s1", sets_root=sets_root, state=state)
    assert len(tl.rows) == 3
    assert [r.start_label for r in tl.rows] == ["00:00", "05:30", "72:05"]


@pytest.mark.requirement(REQ)
def test_duplicate_and_subthreshold_dropped(sets_root: Path) -> None:
    """[if] plays are dupes or below threshold [then] they drop from the list, [else stop]."""
    events = [
        _track_event("s1", 0.0, stable_id="a", title="One"),
        _track_event("s1", 5.0, stable_id="a", title="One"),
        _track_event("s1", 10.0, stable_id="b", title="Two", extra={"audible_s": 1.0, "threshold_s": 5.0}),
    ]
    state, _ = _seed_sqlite_session(sets_root, events=events)
    tl = build_tracklist("s1", sets_root=sets_root, state=state)
    assert len(tl.rows) == 1
    assert render_tracklist(tl, "txt") == "1. One\n"


@pytest.mark.requirement(REQ)
def test_hidden_redaction(sets_root: Path) -> None:
    """[if] stable id is hidden [then] export shows ID - ID, [else stop]."""
    events = [_track_event("s1", 0.0, stable_id="b", title="Secret", artist="X")]
    state, _ = _seed_sqlite_session(sets_root, events=events)
    tl = build_tracklist("s1", sets_root=sets_root, state=state, hidden_stable_ids=frozenset({"b"}))
    assert tl.rows[0].hidden is True
    assert render_tracklist(tl, "txt") == "1. ID - ID\n"


@pytest.mark.requirement(REQ)
def test_renderers_nonempty(sets_root: Path) -> None:
    """[if] a row has a pipe in the title [then] md escapes it, [else stop]."""
    events = [_track_event("s1", 0.0, stable_id="a", title="A|B", artist="Art")]
    state, _ = _seed_sqlite_session(sets_root, events=events)
    tl = build_tracklist("s1", sets_root=sets_root, state=state)
    assert "\\|" in render_tracklist(tl, "md")
    assert render_tracklist(tl, "timestamped").startswith("[00:00]")
    assert "0:00 Art - A|B" in render_tracklist(tl, "soundcloud")


@pytest.mark.requirement(REQ)
def test_json_route_no_file_path(
    api_test_client: tuple[TestClient, Path],
) -> None:
    """[if] GET tracklist JSON [then] rows omit file_path, [else stop]."""
    client, sets_root = api_test_client
    events = [_track_event("s1", 0.0, stable_id="a", title="One", artist="A")]
    _seed_sqlite_session(sets_root, events=events)
    resp = client.get("/api/sets/s1/tracklist")
    assert resp.status_code == 200
    row = resp.json()["rows"][0]
    assert "file_path" not in row


@pytest.mark.requirement(REQ)
def test_m3u8_route_shared_writer(
    api_test_client: tuple[TestClient, Path],
) -> None:
    """[if] GET tracklist.m3u8 [then] body matches shared render_m3u8, [else stop]."""
    client, sets_root = api_test_client
    events = [
        _track_event("s1", 0.0, stable_id="a", title="One", artist="Alice", extra={"duration_ms": 240000}),
        _track_event("s1", 60.0, stable_id="b", title="Two", artist="Bob"),
    ]
    _seed_sqlite_session(sets_root, events=events)

    class FakeBackend:
        def get_tracks_bulk(self, ids: list[str]) -> dict[str, SimpleNamespace]:
            return {
                "a": SimpleNamespace(
                    artist="Alice",
                    title="One",
                    key="8A",
                    duration_ms=240000,
                    file_path="/music/one.mp3",
                ),
                "b": SimpleNamespace(
                    artist="Bob",
                    title="Two",
                    key=None,
                    duration_ms=180000,
                    file_path=None,
                ),
            }

    client.app.state.backend = FakeBackend()
    resp = client.get("/api/sets/s1/tracklist.m3u8")
    assert resp.status_code == 200
    assert "audio/x-mpegurl" in resp.headers["content-type"]
    assert 'filename="s1.m3u8"' in resp.headers.get("content-disposition", "")
    expected = (
        render_m3u8([M3u8Entry(duration_s=240, artist="Alice", title="One", location="/music/one.mp3")]).rstrip("\n")
        + "\n# not in the library: Bob - Two\n"
    )
    assert resp.text == expected


@pytest.mark.requirement(REQ)
def test_soundcloud_export_requires_ack(
    api_test_client: tuple[TestClient, Path],
) -> None:
    """[if] soundcloud fmt without ack [then] 400 carries LICENSING_REMINDER, [else stop]."""
    client, sets_root = api_test_client
    _seed_sqlite_session(
        sets_root,
        events=[_track_event("s1", 0.0, stable_id="a", title="T")],
    )
    resp = client.get("/api/sets/s1/tracklist.soundcloud")
    assert resp.status_code == 400
    assert resp.json()["detail"] == LICENSING_REMINDER


@pytest.mark.requirement(REQ)
def test_openapi_lists_tracklist_paths(api_test_client: tuple[TestClient, Path]) -> None:
    """[if] OpenAPI is read [then] tracklist paths are present, [else stop]."""
    client, _ = api_test_client
    schema = client.app.openapi()["paths"]
    assert "/api/sets/{session_id}/tracklist" in schema
    assert "/api/sets/{session_id}/tracklist.{fmt}" in schema


@pytest.mark.requirement(REQ)
def test_format_start_label() -> None:
    """[if] start_s is 4325 [then] label is 72:05, [else stop]."""
    assert format_start_label(4325.0) == "72:05"


def _write_transitions(sets_root: Path, session_id: str, transitions: list[dict]) -> None:
    sess = sets_root / session_id
    (sess / "transitions.jsonl").write_text(
        "\n".join(json.dumps(t) for t in transitions) + "\n",
        encoding="utf-8",
    )


@pytest.mark.requirement(REQ)
def test_transition_labels_to_track_and_window(sets_root: Path) -> None:
    """[if] transitions match to_track or window [then] labels attach, [else stop]."""
    events = [
        _track_event("s1", 0.0, stable_id="a", title="One"),
        _track_event("s1", 60.0, stable_id="b", title="Two"),
        _track_event("s1", 180.0, stable_id="c", title="Three"),
    ]
    state, _ = _seed_sqlite_session(sets_root, events=events)
    _write_transitions(
        sets_root,
        "s1",
        [
            {"t_change_s": 55.0, "to_track": "b", "predicted_class": "blend"},
            {"t_change_s": 181.0, "predicted_class": "cut"},
        ],
    )
    tl = build_tracklist("s1", sets_root=sets_root, state=state)
    assert tl.rows[0].transition_label == "blend"
    assert tl.rows[1].transition_label == "cut"
    assert tl.rows[2].transition_label is None


@pytest.mark.requirement(REQ)
def test_played_length_sources(sets_root: Path) -> None:
    """[if] plays differ [then] audible_s, next gap, or duration apply, [else stop]."""
    events = [
        _track_event("s1", 0.0, stable_id="a", title="One", extra={"audible_s": 42.0}),
        _track_event("s1", 50.0, stable_id="b", title="Two"),
        _track_event("s1", 200.0, stable_id="c", title="Three"),
    ]
    state, _ = _seed_sqlite_session(
        sets_root,
        events=events,
        duration_ended_at="2026-04-17T22:00:00+00:00",
    )
    tl = build_tracklist("s1", sets_root=sets_root, state=state)
    assert tl.rows[0].played_s == 42.0
    assert tl.rows[1].played_s == 150.0
    assert tl.rows[2].played_s == pytest.approx(1600.0)


@pytest.mark.requirement(REQ)
def test_render_cue_with_and_without_segments(sets_root: Path) -> None:
    """[if] cue export [then] INDEX lines match segments or no-audio form, [else stop]."""
    events = [
        _track_event("s1", 0.0, stable_id="a", title="One", artist="A1"),
        _track_event("s1", 310.0, stable_id="b", title="Two", artist="A2"),
    ]
    state, sess = _seed_sqlite_session(sets_root, events=events)
    tl = build_tracklist("s1", sets_root=sets_root, state=state)
    no_seg = render_tracklist(tl, "cue")
    assert 'FILE "s1.wav" WAVE' in no_seg
    assert "INDEX 01 00:00:00" in no_seg
    assert "INDEX 01 05:10:00" in no_seg

    seg1 = sess / "audio_part1.mp3"
    seg2 = sess / "audio_part2.mp3"
    seg1.write_bytes(b"\x00")
    seg2.write_bytes(b"\x00")
    write_manifest(
        sess,
        Manifest(
            session_id="s1",
            started_at="2026-04-17T21:30:00+00:00",
            ended_at="2026-04-17T23:00:00+00:00",
            capture_device="test",
            event_count=2,
            deck_sources=["opendj"],
            mp3_segments=[
                AudioSegment(name=seg1.name, start_t_s=0.0, duration_s=300.0, size_bytes=1),
                AudioSegment(name=seg2.name, start_t_s=300.0, duration_s=300.0, size_bytes=1),
            ],
        ),
    )
    tl2 = build_tracklist(
        "s1",
        sets_root=sets_root,
        state=state,
        track_lookup=lambda ids: {
            "a": SimpleNamespace(
                artist="A1",
                title="One",
                key=None,
                duration_ms=None,
                file_path="/music/one.mp3",
            ),
            "b": SimpleNamespace(
                artist="A2",
                title="Two",
                key=None,
                duration_ms=None,
                file_path="/music/two.mp3",
            ),
        },
    )
    cue = render_tracklist(tl2, "cue")
    assert 'FILE "audio_part1.mp3" MP3' in cue
    assert 'FILE "audio_part2.mp3" MP3' in cue
    assert "INDEX 01 00:00:00" in cue
    assert "INDEX 01 00:10:00" in cue


@pytest.mark.requirement(REQ)
def test_jsonl_fallback_when_sqlite_has_no_plays(sets_root: Path) -> None:
    """[if] sqlite lacks plays [then] timeline jsonl rows are used, [else stop]."""
    timeline = [
        _play("s1", 0.0, title="FromJsonl", artist="J", track_stable_id="j1"),
    ]
    _seed_session(sets_root, "s1", timeline=timeline)
    state = SetsState(db_path=sets_root / "sets.db")
    state.open_session("s1", capture_device="test", started_at="2026-04-17T21:30:00+00:00")
    tl = build_tracklist("s1", sets_root=sets_root, state=state)
    assert len(tl.rows) == 1
    assert tl.rows[0].title == "FromJsonl"


@pytest.mark.requirement(REQ)
def test_routes_not_found_bad_id_and_unknown_fmt(
    api_test_client: tuple[TestClient, Path],
) -> None:
    """[if] bad session or fmt [then] 404/400/422, [else stop]."""
    client, sets_root = api_test_client
    assert client.get("/api/sets/missing/tracklist").status_code == 404
    with pytest.raises(SessionPathError):
        build_tracklist("bad\x00id", sets_root=sets_root)
    _seed_sqlite_session(
        sets_root,
        events=[_track_event("s1", 0.0, stable_id="a", title="T")],
    )
    assert client.get("/api/sets/s1/tracklist.zzz").status_code == 422


@pytest.mark.requirement(REQ)
def test_m3u8_route_play_order(
    api_test_client: tuple[TestClient, Path],
) -> None:
    """[if] two library tracks [then] m3u8 lists Alice before Bob, [else stop]."""
    client, sets_root = api_test_client
    events = [
        _track_event("s1", 0.0, stable_id="a", title="One", artist="Alice"),
        _track_event("s1", 90.0, stable_id="b", title="Two", artist="Bob"),
    ]
    _seed_sqlite_session(sets_root, events=events)

    class FakeBackend:
        def get_tracks_bulk(self, ids: list[str]) -> dict[str, SimpleNamespace]:
            return {
                "a": SimpleNamespace(
                    artist="Alice",
                    title="One",
                    key=None,
                    duration_ms=120000,
                    file_path="/music/one.mp3",
                ),
                "b": SimpleNamespace(
                    artist="Bob",
                    title="Two",
                    key=None,
                    duration_ms=90000,
                    file_path="/music/two.mp3",
                ),
            }

    client.app.state.backend = FakeBackend()
    resp = client.get("/api/sets/s1/tracklist.m3u8")
    assert resp.status_code == 200
    alice_at = resp.text.index("Alice - One")
    bob_at = resp.text.index("Bob - Two")
    assert alice_at < bob_at
    assert "#EXTINF:120,Alice - One" in resp.text
    assert "/music/one.mp3" in resp.text
