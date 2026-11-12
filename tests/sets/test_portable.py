"""SET-12013: portable set document export and import round-trip.

[if] export then import on empty roots [then] same events and private manifest, [else stop].
[if] serialized export mentions host paths [then] fail portable check, [else stop].
[if] session id collides on import [then] suffix _2 and message, [else stop].
[if] schema_version is newer than reader [then] 422 SCHEMA_VERSION_UNSUPPORTED, [else stop].
[if] recorder.pid is live [then] export 409 SESSION_RECORDING, [else stop].
[if] opendj set export/import [then] match HTTP routes, [else stop].
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, set_cli
from apps.sets import paths as sets_paths_mod
from apps.sets import portable
from apps.sets.manifest import AudioSegment, Manifest, write_manifest
from apps.sets.record import TimelineJsonl
from apps.sets.state import Event, SetsState
from tests.sets.conftest import mount_sets_router

pytestmark = pytest.mark.requirement("SET-12013")

SESSION_ID = "2026-11-08T21-00-00"


def _api_client(sets_root: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", sets_root)
    app = FastAPI()
    mount_sets_router(app)
    return TestClient(app)


def _seed_set(root: Path, session_id: str = SESSION_ID) -> None:
    sess = root / session_id
    sess.mkdir(parents=True)
    mp3 = sess / "audio_seg.mp3"
    mp3.write_bytes(b"\xff\xfb" + b"\x00" * 64)
    write_manifest(
        sess,
        Manifest(
            session_id=session_id,
            started_at="2026-11-08T21:00:00+00:00",
            ended_at="2026-11-08T22:00:00+00:00",
            capture_device="device on TESTHOST7",
            share_state="shared_local",
            event_count=0,
            deck_sources=["djay_monitor"],
            mp3_segments=[AudioSegment(name=mp3.name, start_t_s=0.0, duration_s=1.0, size_bytes=mp3.stat().st_size)],
        ),
    )
    state = SetsState(db_path=root / "sets.db")
    state.open_session(session_id, capture_device="device on TESTHOST7", started_at="2026-11-08T21:00:00+00:00")
    state.end_session(session_id, ended_at="2026-11-08T22:00:00+00:00")
    tl = TimelineJsonl(sess / "timeline.jsonl")

    def _ev(
        *,
        timestamp_s: float,
        action: str,
        track_stable_id: str | None = None,
        value: dict[str, Any] | None = None,
    ) -> None:
        ev = Event(
            session_id=session_id,
            timestamp_s=timestamp_s,
            wall_clock=f"2026-11-08T21:00:{int(timestamp_s):02d}+00:00",
            action=action,
            source="djay_monitor",
            deck="1",
            track_stable_id=track_stable_id,
            value=value or {},
        )
        state.record_event(ev)
        tl.append(ev)

    _ev(timestamp_s=0.0, action="recording_started")
    _ev(
        timestamp_s=1.0,
        action="track_loaded",
        track_stable_id="track-a",
        value={"title": "A Title", "artist": "A Artist"},
    )
    _ev(
        timestamp_s=2.0,
        action="track_loaded",
        track_stable_id="track-a",
        value={"title": "A Title", "artist": "A Artist"},
    )
    _ev(
        timestamp_s=10.0,
        action="track_loaded",
        track_stable_id="track-b",
        value={"title": "B Title", "artist": "B Artist"},
    )
    _ev(timestamp_s=11.0, action="track_change")
    _ev(
        timestamp_s=12.0,
        action="deck_state",
        value={"file_path": f"{root}/x.mp3", "note": "seen on TESTHOST7"},
    )
    (sess / "transitions.jsonl").write_text(
        json.dumps(
            {
                "idx": 0,
                "t_start_s": 0.0,
                "t_change_s": 10.0,
                "t_end_s": 11.0,
                "predicted_class": "cut",
                "confidence": 0.8,
                "model_version": "rules-v0",
                "features": {},
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (sess / "labels.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"idx": 0, "class": "cut", "labeler": "human", "labeled_at": "2026-11-08T22:00:00+00:00"}),
                json.dumps({"idx": 0, "class": "blend", "labeler": "human", "labeled_at": "2026-11-08T22:01:00+00:00"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def identity_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(portable, "machine_identity", lambda: ("TESTHOST7", "USER"))


def test_round_trip_on_empty_root(tmp_path: Path, identity_patch: None) -> None:
    """[if] round trip across two roots [then] events and tracklist match [else stop]."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _seed_set(root_a)
    doc = portable.build_set_document(SESSION_ID, sets_root=root_a)
    portable.import_set_document(doc, sets_root=root_b)
    doc_b = portable.build_set_document(SESSION_ID, sets_root=root_b)
    assert len(doc_b["events"]) == len(doc["events"])
    assert [e["action"] for e in doc_b["events"]] == [e["action"] for e in doc["events"]]
    assert doc_b["tracklist"] == doc["tracklist"]
    assert doc_b["transitions"] == doc["transitions"]
    assert doc_b["labels"] == doc["labels"]
    manifest_b = json.loads((root_b / SESSION_ID / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_b["share_state"] == "private"
    assert manifest_b.get("audio") == "absent"
    assert manifest_b["mp3_segments"] == []
    assert not list((root_b / SESSION_ID).glob("audio_*.mp3"))
    state = SetsState(db_path=root_b / "sets.db")
    row = state.get_session(SESSION_ID)
    assert row is not None
    assert row.share_state == "private"


def test_document_is_portable_and_valid(tmp_path: Path, identity_patch: None) -> None:
    """[if] export text leaks host or paths [then] fail [else stop]."""
    root = tmp_path / "sets"
    _seed_set(root)
    doc = portable.build_set_document(SESSION_ID, sets_root=root)
    text = json.dumps(doc)
    for bad in (str(root), str(root.resolve()), "/Users/", "/home/", "TESTHOST7", "file_path"):
        assert bad not in text
    schema = json.loads(portable.SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(doc)


def test_collision_imports_as_suffix_2_and_says_so(tmp_path: Path, identity_patch: None) -> None:
    """[if] same document imported twice [then] _2 then _3 [else stop]."""
    root = tmp_path / "sets"
    _seed_set(root)
    doc = portable.build_set_document(SESSION_ID, sets_root=root)
    first = portable.import_set_document(doc, sets_root=root)
    assert first.session_id == f"{SESSION_ID}_2"
    assert first.renamed is True
    assert SESSION_ID in first.message and f"{SESSION_ID}_2" in first.message
    second = portable.import_set_document(doc, sets_root=root)
    assert second.session_id == f"{SESSION_ID}_3"
    assert second.renamed is True


def test_newer_schema_version_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_patch: None,
) -> None:
    """[if] POST schema_version 2 [then] 422 and no writes [else stop]."""
    root = tmp_path / "sets"
    root.mkdir()
    client = _api_client(root, monkeypatch)
    doc = {
        "kind": portable.SET_DOCUMENT_KIND,
        "schema_version": 2,
        "exported_at": "2026-11-08T22:00:00+00:00",
        "audio": "absent",
        "manifest": {
            "session_id": "2026-11-08T21-00-00",
            "started_at": "2026-11-08T21:00:00+00:00",
            "ended_at": "2026-11-08T22:00:00+00:00",
            "capture_device": "x",
            "share_state": "private",
            "event_count": 0,
            "deck_sources": [],
            "watermark": "w",
            "notes": None,
            "audio": "absent",
        },
        "events": [],
        "tracklist": [],
        "transitions": [],
        "labels": [],
    }
    response = client.post("/api/sets/import", json=doc)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "SCHEMA_VERSION_UNSUPPORTED"
    assert detail["document_version"] == 2
    assert detail["supported_version"] == 1
    assert "2" in detail["message"] and "1" in detail["message"]
    assert not list(root.iterdir())


def test_invalid_document_is_422_with_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_patch: None,
) -> None:
    """[if] manifest has mp3_segments [then] SET_DOCUMENT_INVALID with path [else stop]."""
    root = tmp_path / "sets"
    root.mkdir()
    client = _api_client(root, monkeypatch)
    _seed_set(root)
    doc = portable.build_set_document(SESSION_ID, sets_root=root)
    doc["manifest"]["mp3_segments"] = []
    response = client.post("/api/sets/import", json=doc)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "SET_DOCUMENT_INVALID"
    assert detail.get("path")


def test_hidden_track_exports_withheld(tmp_path: Path, identity_patch: None) -> None:
    """[if] lookup marks track hidden [then] withheld in file [else stop]."""

    def lookup(ids: list[str]) -> dict[str, portable.TrackInfo]:
        out: dict[str, portable.TrackInfo] = {}
        for sid in ids:
            if sid == "track-b":
                out[sid] = portable.TrackInfo(artist="B Artist", title="B Title", hidden=True)
            else:
                out[sid] = portable.TrackInfo(artist="Lib Artist", title=None, hidden=False)
        return out

    root = tmp_path / "sets"
    _seed_set(root)
    doc = portable.build_set_document(SESSION_ID, sets_root=root, track_lookup=lookup)
    b_rows = [r for r in doc["tracklist"] if r.get("stable_id") == "track-b"]
    assert len(b_rows) == 1
    assert b_rows[0]["hidden"] is True
    assert b_rows[0]["artist"] is None and b_rows[0]["title"] is None
    text = json.dumps(doc)
    assert "B Title" not in text
    for ev in doc["events"]:
        if ev.get("track_stable_id") == "track-b":
            assert "title" not in ev.get("value", {}) and "artist" not in ev.get("value", {})
    a_rows = [r for r in doc["tracklist"] if r.get("stable_id") == "track-a"]
    assert a_rows[0]["hidden"] is False
    assert a_rows[0]["title"] == "A Title"
    Draft202012Validator(json.loads(portable.SCHEMA_PATH.read_text(encoding="utf-8"))).validate(doc)


def test_tracklist_artist_title_scrubbed(tmp_path: Path, identity_patch: None) -> None:
    """[if] tracklist artist/title carry paths or host identity [then] null in export [else stop]."""

    # PRRT_kwDOSEvNd861u6hE
    root = tmp_path / "sets"
    _seed_set(root)
    state = SetsState(db_path=root / "sets.db")
    tl = TimelineJsonl(root / SESSION_ID / "timeline.jsonl")
    private_title = f"{root}/demo.mp3"
    ev = Event(
        session_id=SESSION_ID,
        timestamp_s=20.0,
        wall_clock="2026-11-08T21:00:20+00:00",
        action="track_loaded",
        source="djay_monitor",
        deck="1",
        track_stable_id="track-c",
        value={"title": private_title, "artist": "seen on TESTHOST7"},
    )
    state.record_event(ev)
    tl.append(ev)
    ev = Event(
        session_id=SESSION_ID,
        timestamp_s=21.0,
        wall_clock="2026-11-08T21:00:21+00:00",
        action="track_loaded",
        source="djay_monitor",
        deck="1",
        track_stable_id="track-c",
        value={"title": private_title, "artist": "seen on TESTHOST7"},
    )
    state.record_event(ev)
    tl.append(ev)

    ev = Event(
        session_id=SESSION_ID,
        timestamp_s=22.0,
        wall_clock="2026-11-08T21:00:22+00:00",
        action="track_loaded",
        source="djay_monitor",
        deck="1",
        track_stable_id="track-d",
        value={},
    )
    state.record_event(ev)
    tl.append(ev)

    def lookup(ids: list[str]) -> dict[str, portable.TrackInfo]:
        out: dict[str, portable.TrackInfo] = {}
        for sid in ids:
            if sid == "track-d":
                out[sid] = portable.TrackInfo(
                    artist="Library Artist",
                    title="/Users/USER/Music/x.mp3",
                    hidden=False,
                )
        return out

    doc = portable.build_set_document(SESSION_ID, sets_root=root, track_lookup=lookup)
    c_rows = [r for r in doc["tracklist"] if r.get("stable_id") == "track-c"]
    assert len(c_rows) == 1
    assert c_rows[0]["title"] is None and c_rows[0]["artist"] is None
    assert c_rows[0]["hidden"] is False
    d_rows = [r for r in doc["tracklist"] if r.get("stable_id") == "track-d"]
    assert len(d_rows) == 1
    assert d_rows[0]["title"] is None
    assert d_rows[0]["artist"] == "Library Artist"
    text = json.dumps(doc)
    for bad in (str(root), str(root.resolve()), "TESTHOST7", "demo.mp3"):
        assert bad not in text
    Draft202012Validator(json.loads(portable.SCHEMA_PATH.read_text(encoding="utf-8"))).validate(doc)


def test_manifest_notes_scrubbed(tmp_path: Path, identity_patch: None) -> None:
    """[if] manifest notes carry paths or host identity [then] null in export [else stop]."""

    # PRRT_kwDOSEvNd8617UVT
    root = tmp_path / "sets"
    _seed_set(root)
    db_path = root / "sets.db"

    private_notes = f"recorded from {root}/demo.mp3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE sets SET notes = ? WHERE session_id = ?",
            (private_notes, SESSION_ID),
        )
        conn.commit()
    doc = portable.build_set_document(SESSION_ID, sets_root=root)
    assert doc["manifest"]["notes"] is None
    assert str(root) not in json.dumps(doc)
    Draft202012Validator(json.loads(portable.SCHEMA_PATH.read_text(encoding="utf-8"))).validate(doc)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE sets SET notes = ? WHERE session_id = ?",
            ("warm-up set, good crowd", SESSION_ID),
        )
        conn.commit()
    doc = portable.build_set_document(SESSION_ID, sets_root=root)
    assert doc["manifest"]["notes"] == "warm-up set, good crowd"


def test_export_of_active_recording_is_409(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_patch: None,
) -> None:
    """[if] recorder.pid is live [then] 409; stale pid allows export [else stop]."""
    root = tmp_path / "sets"
    _seed_set(root)
    client = _api_client(root, monkeypatch)
    pid_path = root / SESSION_ID / "recorder.pid"
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    blocked = client.get(f"/api/sets/{SESSION_ID}/export")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "SESSION_RECORDING"
    monkeypatch.setattr(portable, "pid_is_alive", lambda _pid: False)
    allowed = client.get(f"/api/sets/{SESSION_ID}/export")
    assert allowed.status_code == 200


def test_export_route_matches_builder_and_404(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_patch: None,
) -> None:
    """[if] GET export differs from builder or missing session [then] fail [else stop]."""
    root = tmp_path / "sets"
    _seed_set(root)
    client = _api_client(root, monkeypatch)
    built = portable.build_set_document(SESSION_ID, sets_root=root)
    response = client.get(f"/api/sets/{SESSION_ID}/export")
    assert response.status_code == 200
    route_doc = response.json()
    assert route_doc["events"] == built["events"]
    assert client.get("/api/sets/no-such/export").status_code == 404
    assert client.post("/api/sets/import", json=built).status_code == 201


def test_cli_export_import_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_patch: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] CLI export/import diverge from routes [then] fail [else stop]."""
    root = tmp_path / "sets"
    _seed_set(root)
    client = _api_client(root, monkeypatch)

    class _Target:
        base_url = "http://engine.test"

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            served = client.get(request.url.path)
        else:
            body = json.loads(request.content.decode("utf-8")) if request.content else None
            served = client.post(request.url.path, json=body)
        return httpx.Response(served.status_code, content=served.content, headers=dict(served.headers))

    def _fake_http_request(*, lock: object = None) -> tuple[httpx.Client, str]:
        return httpx.Client(
            transport=httpx.MockTransport(_handler), timeout=set_cli.REQUEST_TIMEOUT_S
        ), _Target().base_url

    monkeypatch.setattr(set_cli, "_http_request", _fake_http_request)

    out_file = tmp_path / "set.json"
    assert set_cli.run(["export", SESSION_ID, "--out", str(out_file)], as_json=True) == EXIT_CONFIRMED
    route_doc = client.get(f"/api/sets/{SESSION_ID}/export").json()
    file_doc = json.loads(out_file.read_text(encoding="utf-8"))
    file_doc.pop("exported_at", None)
    route_doc.pop("exported_at", None)
    assert file_doc == route_doc

    capsys.readouterr()
    assert set_cli.run(["import", str(out_file)], as_json=True) == EXIT_CONFIRMED
    imported = json.loads(capsys.readouterr().out)
    assert imported["session_id"] == f"{SESSION_ID}_2"
    assert imported["renamed"] is True

    bad = dict(file_doc)
    bad["schema_version"] = 2
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad), encoding="utf-8")
    assert set_cli.run(["import", str(bad_path)], as_json=True) == EXIT_FAILED

    assert set_cli.run(["export"], as_json=True) == EXIT_FAILED
