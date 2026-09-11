"""POST /api/v1/feedback/comments/{id}/replies: pin follow-up thread (#905).

[if] a follow-up is posted on an open pin [then] text and position are unchanged,
    replies gains one operator turn, and GET /comments returns the full thread
[if] a legacy pin has agent_note but no replies [then] the first operator reply
    seeds the agent note as replies[0] before appending the follow-up
[if] PATCH sets agent_note [then] an agent reply is appended unless it duplicates
    the last agent turn
[if] the id is unknown [then] 404 COMMENT_NOT_FOUND, store unchanged
[if] the body is blank [then] 422, store unchanged
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    from apps.webui.server.app import create_app

    app = create_app(mount_frontend=False)
    app.state.data_dir = tmp_path
    return TestClient(app)


@pytest.fixture()
def http_client(tmp_path: Path) -> TestClient:
    from apps.webui.server.app import create_app

    app = create_app(mount_frontend=False)
    app.state.data_dir = tmp_path
    return TestClient(app, raise_server_exceptions=False)


def _create(client: TestClient, text: str = "hot cues ignore BSM") -> dict:
    r = client.post(
        "/api/v1/feedback/comments",
        json={
            "x_pct": 10,
            "y_pct": 20,
            "anchor": ".bank",
            "page": "/performance",
            "text": text,
            "ui": "chrome-loop",
            "viewport_width": 1280,
            "viewport_height": 800,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _comments(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / "feedback" / "comments.json").read_text())["comments"]


@pytest.mark.requirement("FB-13")
def test_post_reply_on_open_pin(client: TestClient, tmp_path: Path) -> None:
    pin = _create(client)
    r = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/replies",
        json={"text": "still broken on deck 2"},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["text"] == "hot cues ignore BSM"
    assert out["x_pct"] == 10 and out["anchor"] == ".bank"
    assert len(out["replies"]) == 1
    assert out["replies"][0]["author"] == "operator"
    assert out["replies"][0]["text"] == "still broken on deck 2"
    assert out["updated_at"] is not None

    on_disk = _comments(tmp_path)
    assert len(on_disk) == 1
    assert on_disk[0]["replies"][0]["text"] == "still broken on deck 2"

    listed = client.get("/api/v1/feedback/comments").json()["comments"]
    assert len(listed) == 1
    assert listed[0]["replies"][0]["text"] == "still broken on deck 2"


@pytest.mark.requirement("FB-13")
def test_legacy_agent_note_seeded_before_operator_reply(client: TestClient) -> None:
    pin = _create(client)
    client.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={"agent_note": "queued as #1"},
    )
    r = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/replies",
        json={"text": "still broken on deck 2"},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["text"] == "hot cues ignore BSM"
    assert len(out["replies"]) == 2
    assert out["replies"][0]["text"] == "queued as #1"
    assert out["replies"][0]["author"] == "agent"
    assert out["replies"][1]["text"] == "still broken on deck 2"
    assert out["replies"][1]["author"] == "operator"
    assert out["agent_note"] == "queued as #1"


@pytest.mark.requirement("FB-13")
def test_patch_agent_note_appends_reply_without_duplicating(client: TestClient) -> None:
    pin = _create(client)
    r1 = client.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={"agent_note": "queued as #1"},
    )
    assert r1.status_code == 200, r1.text
    assert len(r1.json()["replies"]) == 1

    r2 = client.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={"agent_note": "queued as #1"},
    )
    assert r2.status_code == 200, r2.text
    assert len(r2.json()["replies"]) == 1

    r3 = client.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={"agent_note": "fixed in #2"},
    )
    assert r3.status_code == 200, r3.text
    out = r3.json()
    assert out["agent_note"] == "fixed in #2"
    assert len(out["replies"]) == 2
    assert out["replies"][1]["text"] == "fixed in #2"


@pytest.mark.requirement("FB-13")
def test_reply_unknown_id_is_404(client: TestClient, tmp_path: Path) -> None:
    _create(client)
    before = (tmp_path / "feedback" / "comments.json").read_text()
    r = client.post(
        "/api/v1/feedback/comments/nope/replies",
        json={"text": "hello"},
    )
    assert r.status_code == 404 and r.json()["detail"]["code"] == "COMMENT_NOT_FOUND"
    assert (tmp_path / "feedback" / "comments.json").read_text() == before


@pytest.mark.requirement("FB-13")
def test_reply_rejects_blank_text(client: TestClient, tmp_path: Path) -> None:
    pin = _create(client)
    before = (tmp_path / "feedback" / "comments.json").read_text()
    r = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/replies",
        json={"text": "   "},
    )
    assert r.status_code == 422
    assert (tmp_path / "feedback" / "comments.json").read_text() == before


@pytest.mark.requirement("FB-13")
def test_rejected_reply_leaves_store_readable(http_client: TestClient, tmp_path: Path) -> None:
    bystander = _create(http_client)
    victim = _create(http_client)
    before = (tmp_path / "feedback" / "comments.json").read_text()

    r = http_client.post(
        f"/api/v1/feedback/comments/{victim['id']}/replies",
        json={"text": None},
    )
    assert r.status_code != 200

    after = (tmp_path / "feedback" / "comments.json").read_text()
    assert after == before

    listed = http_client.get("/api/v1/feedback/comments")
    assert listed.status_code == 200
    ids = {c["id"] for c in listed.json()["comments"]}
    assert bystander["id"] in ids and victim["id"] in ids


@pytest.mark.requirement("FB-13")
def test_follow_on_does_not_add_reply_to_parent(client: TestClient) -> None:
    pin = _create(client)
    client.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={"status": "fixed"},
    )
    r = client.post(f"/api/v1/feedback/comments/{pin['id']}/follow-on", json={})
    assert r.status_code == 201, r.text

    parent = client.get("/api/v1/feedback/comments").json()["comments"]
    parent_pin = next(c for c in parent if c["id"] == pin["id"])
    assert parent_pin.get("replies") in (None, [])


@pytest.mark.requirement("FB-13")
def test_reply_never_409s_on_open_pin(client: TestClient) -> None:
    pin = _create(client)
    r = client.post(
        f"/api/v1/feedback/comments/{pin['id']}/replies",
        json={"text": "follow-up while still open"},
    )
    assert r.status_code == 200, r.text


@pytest.mark.requirement("FB-13")
def test_archive_preserves_replies(client: TestClient, tmp_path: Path) -> None:
    pin = _create(client)
    client.post(
        f"/api/v1/feedback/comments/{pin['id']}/replies",
        json={"text": "one more thing"},
    )
    client.patch(f"/api/v1/feedback/comments/{pin['id']}", json={"status": "fixed"})
    r = client.post(f"/api/v1/feedback/comments/{pin['id']}/archive")
    assert r.status_code == 200, r.text

    archives = sorted((tmp_path / "feedback").glob("archive-*.json"))
    assert len(archives) == 1
    archived = json.loads(archives[0].read_text())["comments"][0]
    assert len(archived["replies"]) == 1
    assert archived["replies"][0]["text"] == "one more thing"


@pytest.mark.requirement("FB-13")
def test_list_comments_without_replies_field_still_returns_200(
    client: TestClient, tmp_path: Path
) -> None:
    pin = _create(client)
    path = tmp_path / "feedback" / "comments.json"
    raw = json.loads(path.read_text())
    del raw["comments"][0]["replies"]
    path.write_text(json.dumps(raw, indent=2) + "\n")

    r = client.get("/api/v1/feedback/comments")
    assert r.status_code == 200, r.text
    listed = r.json()["comments"]
    assert listed[0]["id"] == pin["id"]
    assert listed[0]["replies"] == []


pytestmark = pytest.mark.rb_parity
