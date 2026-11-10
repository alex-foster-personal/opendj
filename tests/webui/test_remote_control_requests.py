"""[if] the public /request/api routes break CROWDREQ-10749b [then] fail, [else stop].

CROWDREQ-10749b: catalog, submit with cooldown, vote-once, closed and filtered refusals.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn

from apps.shared.state import db as state_db
from apps.webui.server import crowd_requests as store
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Playlist, Track
from apps.webui.server.remote_control import RemoteSession, build_listener_app
from tests.waits import start_uvicorn_in_thread, stop_uvicorn_in_thread

pytestmark = pytest.mark.requirement("CROWDREQ-10749b")

SESSION = "/request/api/session"
CATALOG = "/request/api/catalog"
REQUESTS = "/request/api/requests"


@dataclass
class Stack:
    engine_url: str
    listener_port: int

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.listener_port}{path}"

    def headers(self) -> dict[str, str]:
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
        yield Stack(engine_url=engine_url, listener_port=listen_port)
    finally:
        if listen_server is not None and listen_thread is not None:
            stop_uvicorn_in_thread(listen_server, listen_thread, what="the remote-control listener")
        stop_uvicorn_in_thread(engine_server, engine_thread, what="the engine")


def _guest(stack: Stack) -> httpx.Client:
    return httpx.Client(trust_env=False, timeout=10, headers=stack.headers())


def _open_session(stack: Stack, *, free_text: bool = True, cooldown_s: int = 180) -> None:
    response = httpx.put(
        f"{stack.engine_url}/api/v1/requests/session",
        json={
            "open": True,
            "catalog_playlist_id": "P1",
            "free_text_allowed": free_text,
            "cooldown_s": cooldown_s,
        },
        timeout=10,
    )
    assert response.status_code == 200, response.text


def test_denylist_matches_stems_at_word_boundaries() -> None:
    """[if] free text contains a denylist stem [then] it is filtered, [else stop]."""
    assert store.text_is_filtered("this is loud")
    assert store.text_is_filtered("Don't be a bitch about it")
    assert not store.text_is_filtered("please play something funky")
    key = store.requester_fingerprint("cookie-value", "127.0.0.1")
    assert key == hashlib.sha256(b"cookie-value127.0.0.1").hexdigest()
    ledger = store.VoteLedger()
    assert ledger.mark("r1", "k1") is True
    assert ledger.already_voted("r1", "k1") is True
    assert ledger.mark("r1", "k1") is False
    assert ledger.already_voted("r1", "k2") is False


def test_closed_routes_are_423_except_session(stack: Stack) -> None:
    """[if] the session is closed [then] every /request/api/* route except session answers 423."""
    with _guest(stack) as guest:
        session = guest.get(stack.url(SESSION))
        assert session.status_code == 200, session.text
        body = session.json()
        assert body["open"] is False
        assert "cooldown_s" in body
        cookie = session.headers.get("set-cookie", "")
        assert "odj_req=" in cookie
        assert "httponly" in cookie.lower()

        catalog = guest.get(stack.url(CATALOG))
        assert catalog.status_code == 423, catalog.text
        assert catalog.json()["code"] == "requests_closed"

        listed = guest.get(stack.url(REQUESTS))
        assert listed.status_code == 423
        assert listed.json()["code"] == "requests_closed"

        posted = guest.post(stack.url(REQUESTS), json={"stable_id": "trk-1", "name": "the maintainer"})
        assert posted.status_code == 423
        assert posted.json()["code"] == "requests_closed"

        voted = guest.post(stack.url(f"{REQUESTS}/missing/vote"))
        assert voted.status_code == 423
        assert voted.json()["code"] == "requests_closed"

        art = guest.get(stack.url("/request/api/artwork/trk-1"))
        assert art.status_code == 423
        assert art.json()["code"] == "requests_closed"


def test_second_submit_within_cooldown_is_429_and_store_holds_one(stack: Stack) -> None:
    """[if] a phone submits twice within the cooldown [then] the second is 429 with retry_after_s."""
    _open_session(stack)
    with _guest(stack) as guest:
        assert guest.get(stack.url(SESSION)).status_code == 200
        first = guest.post(stack.url(REQUESTS), json={"stable_id": "trk-1", "name": "the maintainer"})
        assert first.status_code == 201, first.text
        assert first.json()["id"]
        second = guest.post(stack.url(REQUESTS), json={"stable_id": "trk-2", "name": "the maintainer"})
        assert second.status_code == 429, second.text
        assert second.json()["retry_after_s"] > 0
        assert second.json()["retry_after_s"] <= 180
    stored = httpx.get(f"{stack.engine_url}/api/v1/requests", params={"status": "all"}, timeout=10)
    assert stored.status_code == 200, stored.text
    assert len(stored.json()["requests"]) == 1


def test_first_submit_without_session_sets_cookie_and_cooldown(stack: Stack) -> None:
    """[if] a phone's first call is a submit [then] the cookie is set and a second submit is 429."""
    _open_session(stack)
    with _guest(stack) as guest:
        first = guest.post(stack.url(REQUESTS), json={"stable_id": "trk-1", "name": "the maintainer"})
        assert first.status_code == 201, first.text
        cookie = first.headers.get("set-cookie", "")
        assert "odj_req=" in cookie
        assert "httponly" in cookie.lower()
        assert guest.cookies.get("odj_req")
        second = guest.post(stack.url(REQUESTS), json={"stable_id": "trk-2", "name": "the maintainer"})
        assert second.status_code == 429, second.text
        assert second.json()["retry_after_s"] > 0
    stored = httpx.get(f"{stack.engine_url}/api/v1/requests", params={"status": "all"}, timeout=10)
    assert stored.status_code == 200, stored.text
    assert len(stored.json()["requests"]) == 1


def test_unknown_stable_id_is_not_in_catalog_and_denylist_is_filtered(stack: Stack) -> None:
    """[if] stable_id is not in the catalog [then] 422 not_in_catalog; denylist text is filtered."""
    _open_session(stack)
    with _guest(stack) as guest:
        assert guest.get(stack.url(SESSION)).status_code == 200
        missing = guest.post(stack.url(REQUESTS), json={"stable_id": "trk-nope", "name": "the maintainer"})
        assert missing.status_code == 422, missing.text
        assert missing.json()["code"] == "not_in_catalog"
        filtered = guest.post(stack.url(REQUESTS), json={"text": "this is loud", "name": "the maintainer"})
        assert filtered.status_code == 422, filtered.text
        assert filtered.json()["code"] == "filtered"
    stored = httpx.get(f"{stack.engine_url}/api/v1/requests", params={"status": "all"}, timeout=10)
    assert stored.json()["requests"] == []


def test_free_text_refused_when_not_allowed(stack: Stack) -> None:
    """[if] free text is off [then] a text submit is 422, [else stop]."""
    _open_session(stack, free_text=False)
    with _guest(stack) as guest:
        assert guest.get(stack.url(SESSION)).json()["free_text_allowed"] is False
        refused = guest.post(stack.url(REQUESTS), json={"text": "play something funky", "name": "the maintainer"})
        assert refused.status_code == 422, refused.text
        assert refused.json()["code"] == "free_text_not_allowed"


def test_same_phone_votes_once(stack: Stack) -> None:
    """[if] the same phone votes twice [then] the count rose once."""
    _open_session(stack)
    with _guest(stack) as guest:
        assert guest.get(stack.url(SESSION)).status_code == 200
        created = guest.post(stack.url(REQUESTS), json={"stable_id": "trk-1", "name": "the maintainer"})
        assert created.status_code == 201, created.text
        request_id = created.json()["id"]
        first = guest.post(stack.url(f"{REQUESTS}/{request_id}/vote"))
        assert first.status_code == 200, first.text
        assert first.json()["already_voted"] is False
        assert first.json()["votes"] == 1
        second = guest.post(stack.url(f"{REQUESTS}/{request_id}/vote"))
        assert second.status_code == 200, second.text
        assert second.json()["already_voted"] is True
    stored = httpx.get(f"{stack.engine_url}/api/v1/requests", params={"status": "all"}, timeout=10)
    assert stored.json()["requests"][0]["votes"] == 1


def test_concurrent_submits_from_same_phone_store_one(stack: Stack) -> None:
    """[if] the same phone submits two requests at once [then] one is stored and the other is 429."""
    _open_session(stack)
    with _guest(stack) as guest:
        assert guest.get(stack.url(SESSION)).status_code == 200
        cookie = guest.cookies.get("odj_req")
        assert cookie

    async def run() -> list[int]:
        async with httpx.AsyncClient(
            trust_env=False, timeout=10, headers=stack.headers(), cookies={"odj_req": cookie}
        ) as client:
            first, second = await asyncio.gather(
                client.post(stack.url(REQUESTS), json={"stable_id": "trk-1", "name": "the maintainer"}),
                client.post(stack.url(REQUESTS), json={"stable_id": "trk-2", "name": "the maintainer"}),
            )
            return [first.status_code, second.status_code]

    assert sorted(asyncio.run(run())) == [201, 429]
    stored = httpx.get(f"{stack.engine_url}/api/v1/requests", params={"status": "all"}, timeout=10)
    assert stored.status_code == 200, stored.text
    assert len(stored.json()["requests"]) == 1


def test_concurrent_votes_from_same_phone_count_once(stack: Stack) -> None:
    """[if] the same phone votes twice at once [then] the count rose once."""
    _open_session(stack)
    with _guest(stack) as guest:
        assert guest.get(stack.url(SESSION)).status_code == 200
        created = guest.post(stack.url(REQUESTS), json={"stable_id": "trk-1", "name": "the maintainer"})
        assert created.status_code == 201, created.text
        request_id = created.json()["id"]
        cookie = guest.cookies.get("odj_req")
        assert cookie

    async def run() -> list[dict[str, object]]:
        vote_url = stack.url(f"{REQUESTS}/{request_id}/vote")
        async with httpx.AsyncClient(
            trust_env=False, timeout=10, headers=stack.headers(), cookies={"odj_req": cookie}
        ) as client:
            first, second = await asyncio.gather(client.post(vote_url), client.post(vote_url))
            assert first.status_code == 200, first.text
            assert second.status_code == 200, second.text
            return [first.json(), second.json()]

    bodies = asyncio.run(run())
    flags = sorted(bool(body["already_voted"]) for body in bodies)
    assert flags == [False, True]
    votes = [body.get("votes") for body in bodies if body["already_voted"] is False]
    assert votes == [1]
    stored = httpx.get(f"{stack.engine_url}/api/v1/requests", params={"status": "all"}, timeout=10)
    assert stored.status_code == 200, stored.text
    assert stored.json()["requests"][0]["votes"] == 1


def test_stored_row_has_hashed_key_and_no_address(stack: Stack) -> None:
    """[if] the stored row is read [then] it carries requester_key and no IP or cookie."""
    _open_session(stack)
    with _guest(stack) as guest:
        opened = guest.get(stack.url(SESSION))
        assert opened.status_code == 200, opened.text
        cookie = guest.cookies.get("odj_req")
        assert cookie
        created = guest.post(stack.url(REQUESTS), json={"text": "play something funky", "name": "the maintainer"})
        assert created.status_code == 201, created.text
        public = guest.get(stack.url(REQUESTS))
        assert public.status_code == 200, public.text
        public_rows = public.json()["requests"]
        assert len(public_rows) == 1
        assert public_rows[0]["requester_name"] == "the maintainer"
        assert public_rows[0]["kind"] == "text"
        assert public_rows[0]["text"] == "play something funky"
        assert "requester_key" not in public_rows[0]
    stored = httpx.get(f"{stack.engine_url}/api/v1/requests", params={"status": "all"}, timeout=10)
    assert stored.status_code == 200, stored.text
    row = stored.json()["requests"][0]
    key = row["requester_key"]
    assert len(key) == 64
    assert all(ch in "0123456789abcdef" for ch in key)
    assert key != cookie
    assert "127.0.0.1" not in key
    assert cookie not in key
    dumped = httpx.get(f"{stack.engine_url}/api/v1/requests", params={"status": "all"}, timeout=10).text
    assert cookie not in dumped
    assert "address" not in row


def test_catalog_and_session_and_foreign_artwork(stack: Stack) -> None:
    """[if] the session is open [then] catalog, catalog_name and artwork rules hold."""
    _open_session(stack)
    with _guest(stack) as guest:
        session = guest.get(stack.url(SESSION))
        assert session.status_code == 200, session.text
        body = session.json()
        assert body["open"] is True
        assert body["catalog_name"] == "Friday Night"
        assert body["free_text_allowed"] is True
        assert body["cooldown_s"] == 180
        catalog = guest.get(stack.url(CATALOG))
        assert catalog.status_code == 200, catalog.text
        tracks = catalog.json()["tracks"]
        assert [row["stable_id"] for row in tracks] == ["trk-1", "trk-2"]
        assert tracks[0]["title"] == "Song One"
        assert tracks[0]["artist"] == "Artist A"
        assert tracks[0]["artwork"] == "/request/api/artwork/trk-1"
        foreign = guest.get(stack.url("/request/api/artwork/trk-nope"))
        assert foreign.status_code == 404, foreign.text
        owned = guest.get(stack.url("/request/api/artwork/trk-1"))
        assert owned.status_code != 423
        engine_art = httpx.get(f"{stack.engine_url}/api/v1/tracks/trk-1/artwork", timeout=10)
        assert owned.status_code == engine_art.status_code
