"""Opt-in ListenBrainz submit client against a local HTTP listener (LIBM-12236).

[if] settings enable ListenBrainz with a token [then] only then is a play submitted, [else stop].
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from apps.scrobble.listenbrainz import (
    ListenBrainzClient,
    ListenBrainzConfigError,
    ListenBrainzSubmitError,
    Play,
    build_single_payload,
)
from apps.shared.settings import load_listenbrainz_settings

pytestmark = pytest.mark.requirement("LIBM-12236")

TOKEN: str = "test-token-123"
PLAY: Play = Play(play_id="p1", artist="Artist", title="Title", listened_at=1_700_000_000, duration_ms=180_000)


@dataclass
class Listener:
    """Local stand-in for the documented submit-listens endpoint (synthesized fixture)."""

    status: int = 200
    requests: list[dict[str, object]] = field(default_factory=list)
    url: str = ""


@pytest.fixture
def listener() -> Iterator[Listener]:
    state = Listener()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            state.requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length).decode("utf-8")),
                }
            )
            self.send_response(state.status)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()


def _settings_file(tmp_path: Path, section: dict[str, object] | None) -> Path:
    path = tmp_path / "settings.json"
    if section is not None:
        path.write_text(json.dumps({"listenbrainz": section}), encoding="utf-8")
    return path


@pytest.mark.parametrize("section", [None, {}, {"enabled": False, "token": TOKEN}])
def test_disabled_settings_open_no_connection(tmp_path: Path, listener: Listener, section: dict | None) -> None:
    """[if] ListenBrainz is disabled or unset [then] no HTTP request is made, [else stop]."""
    settings = load_listenbrainz_settings(_settings_file(tmp_path, section))
    assert not settings.enabled
    client = ListenBrainzClient(settings=settings, api_root=listener.url)
    assert client.submit(PLAY, 180_000) is False
    assert listener.requests == []


def test_enabled_submit_sends_token_and_payload(tmp_path: Path, listener: Listener) -> None:
    """[if] enabled with a token [then] the request carries the token header and payload, [else stop]."""
    settings = load_listenbrainz_settings(_settings_file(tmp_path, {"enabled": True, "token": TOKEN}))
    client = ListenBrainzClient(settings=settings, api_root=listener.url)
    assert client.submit(PLAY, 180_000) is True
    assert listener.requests == [
        {"path": "/1/submit-listens", "authorization": f"Token {TOKEN}", "body": build_single_payload(PLAY)}
    ]
    assert client.submit(PLAY, 180_000) is False
    assert len(listener.requests) == 1


def test_failed_submit_is_not_recorded(tmp_path: Path, listener: Listener) -> None:
    """[if] the endpoint returns non-success [then] the play stays pending for retry, [else stop]."""
    listener.status = 401
    settings = load_listenbrainz_settings(_settings_file(tmp_path, {"enabled": True, "token": TOKEN}))
    client = ListenBrainzClient(settings=settings, api_root=listener.url)
    with pytest.raises(ListenBrainzSubmitError):
        client.submit(PLAY, 180_000)
    assert PLAY.play_id not in client.submitted_play_ids
    listener.status = 200
    assert client.submit(PLAY, 180_000) is True
    assert len(listener.requests) == 2


def test_enabled_without_token_refuses_unauthenticated_submit(tmp_path: Path, listener: Listener) -> None:
    """[if] enabled settings have no token [then] the client raises before any request, [else stop]."""
    settings = load_listenbrainz_settings(_settings_file(tmp_path, {"enabled": True}))
    client = ListenBrainzClient(settings=settings, api_root=listener.url)
    with pytest.raises(ListenBrainzConfigError):
        client.submit(PLAY, 180_000)
    assert listener.requests == []
