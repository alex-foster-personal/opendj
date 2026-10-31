"""UIURL-02: GET /api/v1/ui-url names the UI this process serves, or refuses.

[if] a UI is served [then] ui-url names it on opendj.localhost, else 503, [else stop].
[if] the dev frontend port is closed [then] ui-url is 503 UI_NOT_SERVED, [else stop].
[if] the port holds a silent listener or another app [then] ui-url is 503, [else stop].
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.app_wiring import _mount_frontend_or_placeholder
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.ui_url import UI_DOCUMENT_MARKER, resolve_ui_address

pytestmark = pytest.mark.requirement("UIURL-02")

BACKEND_PORT = 8697
FRONTEND_PORT = 9411


def _client(frontend_port: int | None) -> TestClient:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        port=BACKEND_PORT,
        frontend_port=frontend_port,
    )
    return TestClient(app, base_url="http://127.0.0.1")


def _bound_loopback() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return sock


# The real page template the SvelteKit frontend renders, dev and built alike.
APP_HTML = Path(__file__).resolve().parents[2] / "apps/webui/frontend/src/app.html"


@contextmanager
def _http_server(body: bytes, content_type: str = "text/html; charset=utf-8") -> Iterator[int]:
    """A real loopback HTTP server answering every GET with ``body``."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dev_backend_points_at_its_paired_frontend() -> None:
    """[if] the dev frontend serves the Open DJ page [then] ui-url names that port, [else stop]."""
    page = APP_HTML.read_bytes()
    assert UI_DOCUMENT_MARKER.encode() in page
    with _http_server(page) as port:
        client = _client(port)
        body = client.get("/api/v1/ui-url").json()
        assert body == {
            "ui_url": f"http://opendj.localhost:{port}/",
            "loopback_url": f"http://127.0.0.1:{port}/",
            "port": port,
            "served_by": "dev-frontend",
            "named_host": "opendj.localhost",
        }
        assert client.get("/api/v1/health").json()["ui_url"] == body["ui_url"]


def test_a_port_held_by_something_else_is_not_served() -> None:
    """[if] the frontend port holds a silent listener or another app [then] 503, [else stop].

    discussion_r4237290775: a TCP handshake is not the UI.
    """
    listener = _bound_loopback()
    listener.listen(8)
    silent_port = int(listener.getsockname()[1])
    try:
        silent = SimpleNamespace(spa_mounted=False, port=BACKEND_PORT, frontend_port=silent_port)
        assert resolve_ui_address(silent) is None
    finally:
        listener.close()
    other_page = b"<!doctype html><html><body>Some other local service</body></html>"
    with _http_server(other_page) as other_port:
        client = _client(other_port)
        refused = client.get("/api/v1/ui-url")
        assert refused.status_code == 503
        assert refused.json()["detail"]["code"] == "UI_NOT_SERVED"
        assert client.get("/api/v1/health").json()["ui_url"] is None
    with _http_server(APP_HTML.read_bytes(), content_type="text/plain") as plain_port:
        plain = SimpleNamespace(spa_mounted=False, port=BACKEND_PORT, frontend_port=plain_port)
        assert resolve_ui_address(plain) is None


def test_a_closed_dev_frontend_port_is_not_served() -> None:
    """[if] the dev frontend port is closed [then] ui-url is 503 UI_NOT_SERVED, [else stop]."""
    with _http_server(APP_HTML.read_bytes()) as port:
        listening = SimpleNamespace(spa_mounted=False, port=BACKEND_PORT, frontend_port=port)
        address = resolve_ui_address(listening)
        assert address is not None
        assert (address.served_by, address.port) == ("dev-frontend", port)
    closed = SimpleNamespace(spa_mounted=False, port=BACKEND_PORT, frontend_port=port)
    assert resolve_ui_address(closed) is None
    client = _client(port)
    refused = client.get("/api/v1/ui-url")
    assert refused.status_code == 503
    assert refused.json()["detail"]["code"] == "UI_NOT_SERVED"
    assert client.get("/api/v1/health").json()["ui_url"] is None


def test_a_mounted_bundle_makes_the_engine_port_the_address(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    app = FastAPI()
    app.state.port = BACKEND_PORT
    app.state.frontend_port = FRONTEND_PORT
    _mount_frontend_or_placeholder(app, True, tmp_path)
    address = resolve_ui_address(app.state)
    assert address is not None
    assert (address.ui_url, address.served_by) == (
        f"http://opendj.localhost:{BACKEND_PORT}/",
        "engine",
    )


def test_an_empty_bundle_dir_does_not_count_as_serving(tmp_path: Path) -> None:
    app = FastAPI()
    app.state.port = BACKEND_PORT
    app.state.frontend_port = None
    _mount_frontend_or_placeholder(app, True, tmp_path)
    assert app.state.spa_mounted is False
    assert resolve_ui_address(app.state) is None


def test_resolve_needs_a_port_for_the_engine_branch() -> None:
    state = SimpleNamespace(spa_mounted=True, port=None, frontend_port=None)
    assert resolve_ui_address(state) is None


def test_health_does_not_wait_on_a_stalled_dev_frontend() -> None:
    """[if] the frontend port accepts but never answers [then] /health stays under 1 s with ui_url None, [else stop].

    discussion_r4251103691: the probe must not hold the health event loop.
    """
    listener = _bound_loopback()
    listener.listen(8)
    try:
        client = _client(int(listener.getsockname()[1]))
        started = time.monotonic()
        body = client.get("/api/v1/health").json()
        # The stalled probe alone would take 1.5 s; the headroom absorbs a loaded runner.
        assert time.monotonic() - started < 1.4
        assert body["ui_url"] is None
    finally:
        listener.close()
