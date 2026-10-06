"""PATCH /api/v1/feedback/comments/{id}: the agent-native half of the pin lifecycle (#858).

[if] a fix agent PATCHes status/issue_url/agent_note [then] the pin keeps its text and
    position and gains those fields plus updated_at
[if] the id is unknown [then] 404 COMMENT_NOT_FOUND, nothing written
[if] the body carries no field [then] 422 NO_CHANGES
[if] status is not one of the lifecycle states [then] 422
[if] a patch is rejected by CommentOut validation [then] comments.json is untouched and
    GET /comments still lists every pin
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    # Mirrors tests/webui/test_feedback_routes.py: the feedback dir is resolved from
    # app.state.data_dir, never from the environment.
    from apps.webui.server.app import create_app

    app = create_app(mount_frontend=False)
    app.state.data_dir = tmp_path
    return TestClient(app)


@pytest.fixture()
def http_client(tmp_path: Path) -> TestClient:
    """Same app, but surfacing server errors as responses like a real client does,
    instead of re-raising them into the test."""
    from apps.webui.server.app import create_app

    app = create_app(mount_frontend=False)
    app.state.data_dir = tmp_path
    return TestClient(app, raise_server_exceptions=False)


def _create(client: TestClient) -> dict:
    r = client.post(
        "/api/v1/feedback/comments",
        json={
            "x_pct": 10,
            "y_pct": 20,
            "anchor": ".bank",
            "page": "/performance",
            "text": "hot cues ignore BSM",
            "ui": "chrome-loop",
            "viewport_width": 1280,
            "viewport_height": 800,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_patch_records_lifecycle_fields_without_touching_the_pin(
    client: TestClient, tmp_path: Path
) -> None:
    pin = _create(client)
    r = client.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={
            "status": "issued",
            "issue_url": "https://github.com/x/y/issues/1",
            "agent_note": "queued as #1",
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["text"] == "hot cues ignore BSM" and out["x_pct"] == 10 and out["anchor"] == ".bank"
    assert (
        out["status"] == "issued"
        and out["issue_url"].endswith("/issues/1")
        and out["agent_note"] == "queued as #1"
    )
    assert out["updated_at"] is not None, (
        "if a patch leaves no updated_at then nobody can tell when the agent acted - broken"
    )
    on_disk = json.loads((tmp_path / "feedback" / "comments.json").read_text())["comments"]
    assert on_disk[0]["status"] == "issued", (
        "if the patch is not persisted then the pin forgets on reload - broken"
    )
    listed = client.get("/api/v1/feedback/comments").json()["comments"]
    assert listed[0]["agent_note"] == "queued as #1"


def test_agent_authored_pin_keeps_its_author_and_kind(client: TestClient) -> None:
    r = client.post(
        "/api/v1/feedback/comments",
        json={
            "x_pct": 10,
            "y_pct": 20,
            "page": "/performance",
            "text": "red-team finding",
            "ui": "chrome-loop",
            "viewport_width": 1280,
            "viewport_height": 800,
            "author": "agent",
            "agent_kind": "redteam-haiku",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["author"] == "agent"
    assert r.json()["agent_kind"] == "redteam-haiku"

    operator = _create(client)
    assert operator["author"] == "operator"
    assert operator["agent_kind"] is None

    filed = client.patch(
        f"/api/v1/feedback/comments/{r.json()['id']}",
        json={
            "status": "fixed",
            "issue_url": "https://github.com/private_owner/music-dj-tools/issues/940",
        },
    )
    assert filed.status_code == 200, filed.text
    assert filed.json()["author"] == "agent"
    assert filed.json()["agent_kind"] == "redteam-haiku"
    assert filed.json()["issue_url"].endswith("/issues/940")


def test_patch_unknown_id_is_404_and_writes_nothing(client: TestClient, tmp_path: Path) -> None:
    _create(client)
    before = (tmp_path / "feedback" / "comments.json").read_text()
    r = client.patch("/api/v1/feedback/comments/nope", json={"status": "fixed"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "COMMENT_NOT_FOUND"
    assert (tmp_path / "feedback" / "comments.json").read_text() == before


def test_patch_rejects_empty_body_and_unknown_status(client: TestClient) -> None:
    pin = _create(client)
    assert client.patch(f"/api/v1/feedback/comments/{pin['id']}", json={}).status_code == 422
    assert (
        client.patch(f"/api/v1/feedback/comments/{pin['id']}", json={"status": "done"}).status_code
        == 422
    )


def test_blocked_pin_requires_an_actionable_request_and_can_later_unblock(
    client: TestClient,
) -> None:
    pin = _create(client)

    missing_request = client.patch(
        f"/api/v1/feedback/comments/{pin['id']}", json={"status": "blocked"}
    )
    assert missing_request.status_code == 422

    existing_note = client.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={"agent_note": "auth: the maintainer must approve the existing login request."},
    )
    assert existing_note.status_code == 200
    missing_same_patch_request = client.patch(
        f"/api/v1/feedback/comments/{pin['id']}", json={"status": "blocked"}
    )
    assert missing_same_patch_request.status_code == 422

    unclear_request = client.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={"status": "blocked", "agent_note": "I could not work out what you meant."},
    )
    assert unclear_request.status_code == 422

    undocumented_prefix = client.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={"status": "blocked", "agent_note": "auth: the maintainer needs to approve the login request."},
    )
    assert undocumented_prefix.status_code == 422

    blocked = client.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={
            "status": "blocked",
            "agent_note": (
                "auth: the maintainer must provide the Rekordbox login approval. "
                "The import cannot continue without it."
            ),
        },
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["status"] == "blocked"

    unblocked = client.patch(f"/api/v1/feedback/comments/{pin['id']}", json={"status": "issued"})
    assert unblocked.status_code == 200, unblocked.text
    assert unblocked.json()["status"] == "issued", "unblocking must reuse the same pin"


def test_rejected_patch_leaves_the_store_readable(http_client: TestClient, tmp_path: Path) -> None:
    """A patch the response model refuses must not reach disk.

    comments.json is read back through CommentOut on every list, so a single
    unvalidated write makes GET /comments fail for EVERY pin in the file, not
    just the patched one. ``text: null`` is the reachable trigger: the published
    contract types it ``text?: string | null``, so a typed client can send it.
    """
    bystander = _create(http_client)
    victim = _create(http_client)
    before = (tmp_path / "feedback" / "comments.json").read_text()

    r = http_client.patch(f"/api/v1/feedback/comments/{victim['id']}", json={"text": None})
    assert r.status_code != 200, "if a null text is accepted then CommentOut.text is a lie - broken"

    after = (tmp_path / "feedback" / "comments.json").read_text()
    assert after == before, (
        "if a rejected patch still writes then one bad request corrupts the store - broken"
    )

    listed = http_client.get("/api/v1/feedback/comments")
    assert listed.status_code == 200, (
        "if the list 500s after a rejected patch then the widget is bricked - broken"
    )
    ids = {c["id"] for c in listed.json()["comments"]}
    assert bystander["id"] in ids and victim["id"] in ids, (
        "if a pin vanished then the rejected patch ate data - broken"
    )


pytestmark = pytest.mark.rb_parity
