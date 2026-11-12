"""[if] the phone Requests tab or /remote/api/requests forwards break CROWDREQ-10761 [then] fail, [else stop].

CROWDREQ-10761: paired list of new requests, dismiss, loaded, and session updates.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Playlist, Track
from apps.webui.server.remote_control import PAIR_PATH, RemoteSession, build_listener_app
from tests.waits import start_uvicorn_in_thread, stop_uvicorn_in_thread

pytestmark = pytest.mark.requirement("CROWDREQ-10761")

REQUESTS = "/remote/api/requests"
SESSION = "/remote/api/requests/session"


@dataclass
class Stack:
    engine_url: str
    listener_port: int
    session: RemoteSession

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.listener_port}{path}"

    def host_headers(self) -> dict[str, str]:
        return {"host": f"127.0.0.1:{self.listener_port}"}


def _bind() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    return sock, int(sock.getsockname()[1])


def _seed_backend() -> InMemoryBackend:
    backend = InMemoryBackend()
    now = "2026-11-09T20:00:00.000000Z"
    backend.seed_track(Track(stable_id="trk-1", title="Song One", artist="Artist A", created_at=now, updated_at=now))
    backend.seed_track(Track(stable_id="trk-2", title="Song Two", artist="Artist B", created_at=now, updated_at=now))
    backend.seed_playlist(
        Playlist(
            playlist_id="P1",
            name="Friday Night",
            vendor="rekordbox",
            items=["trk-1", "trk-2"],
            created_at=now,
            updated_at=now,
        )
    )
    return backend


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    data_dir = tmp_path / "data"
    state_path = data_dir / "state" / "state.db"
    state_db.open_rw(state_path).close()
    app = create_app(
        backend=_seed_backend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
        enable_cors=False,
        state_db_path=str(state_path),
    )
    app.state.data_dir = data_dir
    engine_sock, engine_port = _bind()
    engine_server, engine_thread = start_uvicorn_in_thread(
        uvicorn.Config(app, log_level="warning"), what="the engine", sockets=[engine_sock]
    )
    engine_url = f"http://127.0.0.1:{engine_port}"
    listen_server = listen_thread = None
    try:
        listen_sock, listen_port = _bind()
        session = RemoteSession(
            state_dir=tmp_path / "remote",
            requested_port=listen_port,
            engine_url=engine_url,
        )
        session.port = listen_port
        session.address = "127.0.0.1"
        listener_app = build_listener_app(session, engine_url)
        listen_server, listen_thread = start_uvicorn_in_thread(
            uvicorn.Config(listener_app, log_level="warning"),
            what="the remote-control listener",
            sockets=[listen_sock],
        )
        yield Stack(engine_url=engine_url, listener_port=listen_port, session=session)
    finally:
        if listen_server is not None and listen_thread is not None:
            stop_uvicorn_in_thread(listen_server, listen_thread, what="the remote-control listener")
        stop_uvicorn_in_thread(engine_server, engine_thread, what="the engine")


def _pair(stack: Stack) -> dict[str, str]:
    headers = stack.host_headers()
    paired = httpx.post(
        stack.url(PAIR_PATH),
        json={"code": stack.session.display_code, "device_name": "the maintainer phone"},
        headers=headers,
        timeout=10,
    )
    assert paired.status_code == 201, paired.text
    token = paired.json()["token"]
    return {**headers, "authorization": f"Bearer {token}"}


def _engine_open(stack: Stack) -> None:
    response = httpx.put(
        f"{stack.engine_url}/api/v1/requests/session",
        json={"open": True, "catalog_playlist_id": "P1", "free_text_allowed": True, "cooldown_s": 180},
        timeout=10,
    )
    assert response.status_code == 200, response.text


def _engine_add(stack: Stack, **body: str) -> dict:
    payload = {"requester_key": body.pop("requester_key", "guest-key-1"), **body}
    response = httpx.post(f"{stack.engine_url}/api/v1/requests", json=payload, timeout=10)
    assert response.status_code == 201, response.text
    return response.json()


def test_missing_or_unknown_token_is_401(stack: Stack) -> None:
    """[if] the token is missing or unknown [then] the phone request routes answer 401, [else stop]."""
    headers = stack.host_headers()
    listed = httpx.get(stack.url(REQUESTS), headers=headers, timeout=10)
    assert listed.status_code == 401, listed.text
    headers["authorization"] = "Bearer not-a-real-token"
    listed = httpx.get(stack.url(REQUESTS), headers=headers, timeout=10)
    assert listed.status_code == 401, listed.text


def test_list_shows_names_and_votes_and_hides_requester_key(stack: Stack) -> None:
    """[if] a paired phone GETs /remote/api/requests [then] new rows include names and votes and omit requester_key, [else stop]."""
    _engine_open(stack)
    created = _engine_add(stack, stable_id="trk-1", requester_name="Sam", requester_key="secret-key")
    voted = httpx.post(f"{stack.engine_url}/api/v1/requests/{created['id']}/vote", timeout=10)
    assert voted.status_code == 200, voted.text
    headers = _pair(stack)
    listed = httpx.get(stack.url(REQUESTS), headers=headers, timeout=10)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert "secret-key" not in listed.text
    assert "requester_key" not in listed.text
    rows = body["requests"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == created["id"]
    assert row["requester_name"] == "Sam"
    assert row["votes"] == 1
    assert "requester_key" not in row
    assert row["title"] == "Song One"
    assert row["artist"] == "Artist A"


def test_text_request_keeps_the_message_and_omits_title(stack: Stack) -> None:
    """[if] a free-text request is new [then] the phone list shows the text and requester name, [else stop]."""
    _engine_open(stack)
    created = _engine_add(stack, text="play something funky", requester_name="Kim", requester_key="text-key")
    headers = _pair(stack)
    listed = httpx.get(stack.url(REQUESTS), headers=headers, timeout=10)
    assert listed.status_code == 200, listed.text
    assert "text-key" not in listed.text
    row = listed.json()["requests"][0]
    assert row["id"] == created["id"]
    assert row["kind"] == "text"
    assert row["text"] == "play something funky"
    assert row["requester_name"] == "Kim"
    assert "requester_key" not in row


def test_dismiss_and_loaded_leave_the_new_list(stack: Stack) -> None:
    """[if] a paired phone posts :loaded then :dismiss [then] those rows leave GET ?status=new, [else stop]."""
    _engine_open(stack)
    first = _engine_add(stack, stable_id="trk-1", requester_name="Sam", requester_key="k1")
    second = _engine_add(stack, text="one more", requester_name="Kim", requester_key="k2")
    headers = _pair(stack)
    loaded = httpx.post(stack.url(f"{REQUESTS}/{first['id']}:loaded"), headers=headers, json={}, timeout=10)
    assert loaded.status_code == 200, loaded.text
    assert "requester_key" not in loaded.text
    assert loaded.json()["status"] == "loaded"
    dismissed = httpx.post(stack.url(f"{REQUESTS}/{second['id']}:dismiss"), headers=headers, json={}, timeout=10)
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["status"] == "dismissed"
    listed = httpx.get(stack.url(REQUESTS), headers=headers, timeout=10)
    assert listed.status_code == 200, listed.text
    assert listed.json()["requests"] == []


def test_unknown_id_is_404_request_not_found(stack: Stack) -> None:
    """[if] an unknown id is posted [then] 404 with error request_not_found, [else stop]."""
    headers = _pair(stack)
    missing = httpx.post(stack.url(f"{REQUESTS}/no-such-id:dismiss"), headers=headers, json={}, timeout=10)
    assert missing.status_code == 404, missing.text
    body = missing.json()
    assert body["error"] == "request_not_found"


def test_put_session_forwards_open_and_close(stack: Stack) -> None:
    """[if] a paired phone PUTs /remote/api/requests/session [then] the engine session updates, [else stop]."""
    headers = _pair(stack)
    opened = httpx.put(
        stack.url(SESSION),
        headers=headers,
        json={"open": True, "catalog_playlist_id": "P1", "free_text_allowed": True},
        timeout=10,
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["open"] is True
    assert opened.json()["catalog_playlist_id"] == "P1"
    engine = httpx.get(f"{stack.engine_url}/api/v1/requests/session", timeout=10)
    assert engine.status_code == 200, engine.text
    assert engine.json()["open"] is True
    read = httpx.get(stack.url(SESSION), headers=headers, timeout=10)
    assert read.status_code == 200, read.text
    assert read.json()["open"] is True
    closed = httpx.put(stack.url(SESSION), headers=headers, json={"open": False}, timeout=10)
    assert closed.status_code == 200, closed.text
    assert closed.json()["open"] is False


def test_load_command_forwards_to_engine(stack: Stack) -> None:
    """[if] a paired phone POSTs load to /remote/api/command [then] the listener forwards it, [else stop]."""
    headers = _pair(stack)
    command = httpx.post(
        stack.url("/remote/api/command"),
        headers=headers,
        json={"single": {"type": "load", "deck": 1, "stable_id": "trk-1"}},
        timeout=10,
    )
    assert command.status_code != 404, command.text
    assert command.status_code != 401, command.text


def test_remote_page_serves_the_requests_tab(stack: Stack) -> None:
    """[if] a phone opens /remote [then] the page and Requests tab assets are served, [else stop]."""
    headers = stack.host_headers()
    page = httpx.get(stack.url("/remote"), headers=headers, timeout=10)
    assert page.status_code == 200, page.text
    assert "text/html" in page.headers.get("content-type", "")
    assert "Requests" in page.text or "requests-tab" in page.text
    asset = httpx.get(stack.url("/remote/requests-tab-render.js"), headers=headers, timeout=10)
    assert asset.status_code == 200, asset.text
    assert "renderRequestsTab" in asset.text
