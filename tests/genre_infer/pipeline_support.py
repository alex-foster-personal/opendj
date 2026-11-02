"""Shared fixtures for the genre pipeline suites: a small library and a real local HTTP server.

The server answers the three provider routes with replies captured live from
Serper and OpenRouter (``(private path)``, manifest-verified), and
counts every request, so a test can prove a cached search was NOT paid for again.
``fail`` makes a route answer HTTP 500, the only behavior that is not a capture.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from apps.genre_infer import providers
from tests.genre_infer.captured_fixture import load_captured
from tests.webui.library_wheel_fixtures import _make_state_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "genre_pipeline"
ROUTES = {"/search": "serper_search", "/alpha/decisions": "jev_decision", "/v1/chat/completions": "deepseek_chat"}


def captured(name: str) -> dict[str, Any]:
    return load_captured(FIXTURES, name)


@dataclass
class ProviderServer:
    base: str
    hits: Counter[str] = field(default_factory=Counter)
    bodies: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    fail: set[str] = field(default_factory=set)
    # path -> function applied to the captured reply before it is served (a
    # malformed-reply test derives its body from the real capture).
    transform: dict[str, Any] = field(default_factory=dict)


@contextmanager
def provider_server() -> Iterator[ProviderServer]:
    state = ProviderServer(base="")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            name = ROUTES.get(self.path)
            state.hits[self.path] += 1
            state.bodies.setdefault(self.path, []).append(body)
            if name is None or self.path in state.fail:
                self.send_response(404 if name is None else 500)
                self.end_headers()
                return
            reply = captured(name)["response"]
            if self.path in state.transform:
                reply = state.transform[self.path](reply)
            payload = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def providers_live(monkeypatch: pytest.MonkeyPatch) -> Iterator[ProviderServer]:
    """Keys set and every provider URL pointed at the local server."""
    with provider_server() as srv:
        monkeypatch.setenv(providers.OPENROUTER_KEY_ENV, "test-openrouter-key")
        monkeypatch.setenv(providers.SERPER_KEY_ENV, "test-serper-key")
        monkeypatch.setenv(providers.OPENROUTER_ROOT_ENV, srv.base)
        monkeypatch.setenv(providers.SERPER_URL_ENV, srv.base + "/search")
        monkeypatch.setattr(providers, "MAX_ATTEMPTS", 1)
        yield srv


#: Ids of :func:`library`'s tracks, for suites that name them without repeating them:
#: the track the captured provider replies are about, another track by the same
#: artist, the placeholder (``ID`` by ``ID``) and the one that can point at real audio.
ASKED_ID, SAME_ARTIST_ID, PLACEHOLDER_ID, COPY_ID = "t-fred", "t-fred2", "t-id", "t-copy"


def library(tmp_path: Path, *, audio: Path | None = None) -> tuple[Path, Path]:
    """(data_dir, state.db) with four tracks; ``t-copy`` points at ``audio`` when given."""
    data = tmp_path / "data"
    state = data / "state" / "state.db"
    state.parent.mkdir(parents=True)
    _make_state_db(
        state,
        tracks=[
            {
                "stable_id": "t-fred",
                "title": "Delilah (pull me out of this)",
                "artists": ["Fred again.."],
                "file_path": "/m/fred.mp3",
            },
            {"stable_id": "t-fred2", "title": "Jungle", "artists": ["Fred again.."], "file_path": "/m/jungle.mp3"},
            {"stable_id": "t-id", "title": "ID", "artists": ["ID"], "file_path": "/m/id.mp3"},
            {
                "stable_id": "t-copy",
                "title": "Copy",
                "artists": ["Someone"],
                "file_path": str(audio) if audio else "/m/copy.mp3",
            },
        ],
        memberships=[],
        playlists=[],
        vendor_ids={},
        track_fields=[
            {"stable_id": "t-fred", "field_name": "bpm", "value_json": "120"},
            {"stable_id": "t-fred2", "field_name": "genre", "value_json": '"Electronic"'},
        ],
    )
    return data, state
