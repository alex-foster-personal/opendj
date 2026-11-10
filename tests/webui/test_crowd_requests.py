"""[if] the crowd request store or /api/v1/requests routes break CROWDREQ-10749 [then] fail, [else stop].

CROWDREQ-10749: session, request list, vote, dismiss, loaded and clear.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared import events
from apps.shared.state import db as state_db
from apps.webui.server import crowd_requests as store
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("CROWDREQ-10749")

SESSION = "/api/v1/requests/session"
REQUESTS = "/api/v1/requests"
KEY = "guest-key-1"


class _Hub:
    def __init__(self) -> None:
        self.seen: list[tuple[str, dict[str, object]]] = []

    def publish(self, topic: str, payload: dict[str, object]) -> None:
        self.seen.append((topic, payload))


@pytest.fixture
def db(tmp_path: Path) -> Iterator:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    try:
        store.ensure_schema(conn)
        yield conn
    finally:
        conn.close()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    data_dir = tmp_path / "data"
    state_path = data_dir / "state" / "state.db"
    state_db.open_rw(state_path).close()
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
        state_db_path=str(state_path),
    )
    app.state.data_dir = data_dir
    hub = _Hub()
    events.set_hub(hub)
    app.state.crowd_request_hub = hub
    with TestClient(app) as test_client:
        yield test_client
    events.set_hub(None)


def _body(stable_id: str = "trk-1", name: str = "the maintainer") -> dict[str, str]:
    return {"stable_id": stable_id, "requester_name": name, "requester_key": KEY}


def test_store_refuses_open_without_catalog_or_free_text(db) -> None:
    with pytest.raises(store.SessionError) as caught:
        store.set_session(db, open=True)
    assert caught.value.code == "nothing_to_request"


def test_store_opens_with_catalog_and_defaults(db) -> None:
    session = store.set_session(db, open=True, catalog_playlist_id="P1")
    assert session.open is True
    assert session.catalog_playlist_id == "P1"
    assert session.free_text_allowed is False
    assert session.cooldown_s == 180
    assert session.opened_at
    assert store.get_session(db) == session


def test_store_add_lists_new_first(db) -> None:
    first = store.add_request(db, stable_id="trk-old", requester_name="Sam", requester_key=KEY)
    second = store.add_request(db, text="play something funky", requester_name="Riley", requester_key=KEY)
    listed = store.list_requests(db, status="new")
    assert [row.id for row in listed] == [second.id, first.id]
    assert listed[0].kind == "text"
    assert listed[0].text == "play something funky"
    assert listed[0].status == "new"
    assert listed[0].votes == 0
    assert listed[1].kind == "track"
    assert listed[1].stable_id == "trk-old"


def test_store_vote_and_status_and_clear(db) -> None:
    row = store.add_request(db, stable_id="trk-1", requester_name="the maintainer", requester_key=KEY)
    voted = store.vote(db, row.id)
    assert voted is not None
    assert voted.votes == 1
    loaded = store.set_status(db, row.id, "loaded")
    assert loaded is not None
    assert loaded.status == "loaded"
    assert loaded.status_at >= row.status_at
    dismissed = store.set_status(db, row.id, "dismissed")
    assert dismissed is not None
    assert dismissed.status == "dismissed"
    assert store.list_requests(db, status="new") == []
    assert [item.id for item in store.list_requests(db, status="dismissed")] == [row.id]
    assert store.clear_requests(db) == 1
    assert store.list_requests(db, status="all") == []


def test_put_session_without_catalog_or_text_is_422(client: TestClient) -> None:
    response = client.put(SESSION, json={"open": True})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"] == "nothing_to_request"
    assert body["message"]


def test_post_lists_new_first_and_fires_added_once(client: TestClient) -> None:
    opened = client.put(SESSION, json={"open": True, "catalog_playlist_id": "P1"})
    assert opened.status_code == 200, opened.text
    assert opened.json()["open"] is True
    assert opened.json()["catalog_playlist_id"] == "P1"

    first = client.post(REQUESTS, json=_body("trk-a", "Sam"))
    second = client.post(REQUESTS, json=_body("trk-b", "Riley"))
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["status"] == "new"
    assert first.json()["requester_name"] == "Sam"
    assert first.json()["requester_key"] == KEY
    assert "address" not in first.json()

    listed = client.get(REQUESTS)
    assert listed.status_code == 200, listed.text
    rows = listed.json()["requests"]
    assert [row["stable_id"] for row in rows] == ["trk-b", "trk-a"]
    assert all(row["status"] == "new" for row in rows)

    hub: _Hub = client.app.state.crowd_request_hub
    added = [payload for topic, payload in hub.seen if topic == "crowd_request.added"]
    assert [payload["id"] for payload in added] == [first.json()["id"], second.json()["id"]]


def test_added_event_is_visible_when_published(client: TestClient) -> None:
    """A subscriber that refetches on crowd_request.added must see the new row."""
    visible: list[list[str]] = []
    hub: _Hub = client.app.state.crowd_request_hub
    original = hub.publish

    def _on_publish(topic: str, payload: dict[str, object]) -> None:
        original(topic, payload)
        if topic == "crowd_request.added":
            rows = client.get(REQUESTS).json()["requests"]
            visible.append([str(row["id"]) for row in rows])

    hub.publish = _on_publish  # type: ignore[method-assign]
    created = client.post(REQUESTS, json=_body())
    assert created.status_code == 201, created.text
    assert visible == [[created.json()["id"]]]


def test_loaded_then_dismiss_leave_new_empty(client: TestClient) -> None:
    created = client.post(REQUESTS, json=_body())
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    created_at = created.json()["status_at"]

    loaded = client.post(f"{REQUESTS}/{request_id}:loaded")
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["status"] == "loaded"
    assert loaded.json()["status_at"] >= created_at

    dismissed = client.post(f"{REQUESTS}/{request_id}:dismiss")
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["status"] == "dismissed"
    assert dismissed.json()["status_at"] >= loaded.json()["status_at"]

    remaining = client.get(REQUESTS, params={"status": "new"})
    assert remaining.status_code == 200, remaining.text
    assert remaining.json()["requests"] == []
    kept = client.get(REQUESTS, params={"status": "dismissed"})
    assert [row["id"] for row in kept.json()["requests"]] == [request_id]


def test_delete_reports_removed_count_and_empties_the_table(client: TestClient) -> None:
    assert client.post(REQUESTS, json=_body("trk-1")).status_code == 201
    assert client.post(REQUESTS, json=_body("trk-2")).status_code == 201
    cleared = client.delete(REQUESTS)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["removed"] == 2
    assert client.get(REQUESTS, params={"status": "all"}).json()["requests"] == []


def test_vote_increments_and_missing_request_is_404(client: TestClient) -> None:
    created = client.post(REQUESTS, json=_body())
    request_id = created.json()["id"]
    voted = client.post(f"{REQUESTS}/{request_id}/vote")
    assert voted.status_code == 200, voted.text
    assert voted.json()["votes"] == 1
    again = client.post(f"{REQUESTS}/{request_id}/vote")
    assert again.json()["votes"] == 2
    missing = client.post(f"{REQUESTS}/no-such:dismiss")
    assert missing.status_code == 404, missing.text
    assert missing.json()["error"] == "request_not_found"


def test_get_session_defaults_when_unset(client: TestClient) -> None:
    response = client.get(SESSION)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "open": False,
        "catalog_playlist_id": None,
        "free_text_allowed": False,
        "cooldown_s": 180,
        "opened_at": None,
    }
