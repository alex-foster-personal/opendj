"""HTTP contract for SET-06a metadata-only SoundCloud export."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets import api as api_mod
from apps.sets import paths as sets_paths_mod
from apps.sets.soundcloud_export import LICENSING_REMINDER, build_soundcloud_export
from tests.sets.test_soundcloud_export import THREE_TRACK_TIMELINE, _seed_session


@pytest.fixture
def api_test_client(
    sets_root: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Path]]:
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", sets_root)
    app = FastAPI()
    app.include_router(api_mod.router)
    with TestClient(app) as client:
        yield client, sets_root


@pytest.mark.requirement("SET-06a")
def test_get_returns_json_tracklist_not_audio(
    api_test_client: tuple[TestClient, Path],
) -> None:
    client, sets_root = api_test_client
    _seed_session(sets_root, timeline=THREE_TRACK_TIMELINE)
    resp = client.get("/api/sets/s1/soundcloud-export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    payload = resp.json()
    library = build_soundcloud_export("s1", sets_root=sets_root)
    assert payload["licensing_reminder"] == LICENSING_REMINDER
    assert payload["comment"] == library.comment
    assert payload["tracklist"][0]["display_name"] == "Artist One - First Tune"
    assert payload["kind"] == "metadata_only"
    assert payload["audio_upload"] == "not_offered"
    assert payload["takeover"] == "not_offered"
    assert payload["rights_position"] == "unsettled"
    assert "upload_url" not in payload
    assert "player_url" not in payload
    assert "takeover_url" not in payload
    assert not resp.content.startswith(b"\xff\xfb")


@pytest.mark.requirement("SET-06a")
def test_post_without_ack_is_400_and_omits_comment(
    api_test_client: tuple[TestClient, Path],
) -> None:
    client, sets_root = api_test_client
    _seed_session(sets_root, timeline=THREE_TRACK_TIMELINE)
    resp = client.post("/api/sets/s1/soundcloud-export", json={})
    assert resp.status_code == 400
    payload = resp.json()
    assert payload["licensing_reminder"] == LICENSING_REMINDER
    assert payload.get("comment") in (None, "")
    if payload.get("comment"):
        raise AssertionError("paste-ready comment must not be handed over without ack")
    assert "First Tune" not in json.dumps({"comment": payload.get("comment")})


@pytest.mark.requirement("SET-06a")
def test_post_false_ack_is_400_and_omits_comment(
    api_test_client: tuple[TestClient, Path],
) -> None:
    client, sets_root = api_test_client
    _seed_session(sets_root, timeline=THREE_TRACK_TIMELINE)
    resp = client.post(
        "/api/sets/s1/soundcloud-export", json={"acknowledge_rights": False}
    )
    assert resp.status_code == 400
    payload = resp.json()
    assert payload.get("comment") in (None, "")


@pytest.mark.requirement("SET-06a")
def test_post_with_ack_returns_library_comment(
    api_test_client: tuple[TestClient, Path],
) -> None:
    client, sets_root = api_test_client
    _seed_session(sets_root, timeline=THREE_TRACK_TIMELINE)
    resp = client.post(
        "/api/sets/s1/soundcloud-export", json={"acknowledge_rights": True}
    )
    assert resp.status_code == 200
    payload = resp.json()
    library = build_soundcloud_export("s1", sets_root=sets_root)
    assert payload["acknowledged"] is True
    assert payload["comment"] == library.comment
    assert payload["audio_upload"] == "not_offered"
    assert payload["takeover"] == "not_offered"


@pytest.mark.requirement("SET-06a")
def test_get_404_for_missing_session(
    api_test_client: tuple[TestClient, Path],
) -> None:
    client, _ = api_test_client
    resp = client.get("/api/sets/nope/soundcloud-export")
    assert resp.status_code == 404


@pytest.mark.requirement("SET-06a")
@pytest.mark.parametrize(
    ("encoded_session_id", "expected_status"),
    [("..%5Coutside", 400), ("..%2Foutside", 404)],
)
def test_get_rejects_encoded_session_path_traversal(
    api_test_client: tuple[TestClient, Path],
    encoded_session_id: str,
    expected_status: int,
) -> None:
    client, sets_root = api_test_client
    outside = sets_root.parent / "outside"
    outside.mkdir()
    (outside / "manifest.json").write_text("{}", encoding="utf-8")
    (outside / "timeline.jsonl").write_text('{"outside":true}\n', encoding="utf-8")

    response = client.get(f"/api/sets/{encoded_session_id}/soundcloud-export")
    assert response.status_code == expected_status
    if expected_status == 400:
        assert "session_id" in response.json()["detail"]


@pytest.mark.requirement("SET-06a")
def test_get_rejects_encoded_nul_as_client_error(
    api_test_client: tuple[TestClient, Path],
) -> None:
    client, _ = api_test_client
    response = client.get("/api/sets/bad%00id/soundcloud-export")
    assert response.status_code == 400
    assert "session_id" in response.json()["detail"]


@pytest.mark.requirement("SET-06a")
def test_openapi_has_export_paths_and_no_upload(
    api_test_client: tuple[TestClient, Path],
) -> None:
    client, _ = api_test_client
    app = client.app
    assert isinstance(app, FastAPI)
    schema = app.openapi()
    paths = schema["paths"]
    export_path = "/api/sets/{session_id}/soundcloud-export"
    assert export_path in paths
    assert "get" in paths[export_path]
    assert "post" in paths[export_path]
    assert not any("upload" in path for path in paths)
    assert not any("takeover" in path for path in paths)
