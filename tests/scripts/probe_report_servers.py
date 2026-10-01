"""Real loopback servers the RED-trend report tests drive.

Nothing here patches ``urlopen`` or the function under test. Each helper is a
real listener on 127.0.0.1 that the probe reaches over a real socket:

* :func:`serving_engine` -- the production FastAPI app under uvicorn, so the
  client-error route that answers is the one that ships;
* :class:`ReplyDelayingProxy` -- a reverse proxy in front of it that forwards
  the request at once and then HOLDS one reply, which is the shape of a loaded
  host: the engine has already written the row when the client is still
  waiting to hear about it;
* :class:`StallingEngine` -- an endpoint that never finishes a reply, for the
  bound.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

LOOPBACK: str = "127.0.0.1"
HEALTH_PATH: str = "/api/v1/health"
CLIENT_ERRORS_PATH: str = "/api/v1/client-errors"

#: How long a test waits for uvicorn to report it is serving. Generous on
#: purpose: these tests exist because a loaded host is slow, and a one-second
#: startup wait would fail them for the very reason they are written.
ENGINE_START_WAIT_SECONDS: float = 60.0
ENGINE_START_POLL_SECONDS: float = 0.02
ENGINE_STOP_WAIT_SECONDS: float = 10.0
#: Upstream wait inside the proxy. Not under test; only keeps a broken
#: upstream from hanging the proxy thread forever.
PROXY_UPSTREAM_TIMEOUT_SECONDS: float = 60.0

HEALTH_BODY: bytes = b'{"status":"ok","version":"test"}'


def serving_engine(log_dir: Path) -> tuple[uvicorn.Server, threading.Thread, int]:
    """Serve the production client-error route over a real loopback socket."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((LOOPBACK, 0))
    listener.listen(5)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(
                backend=InMemoryBackend(),
                mount_frontend=False,
                enable_cors=False,
                client_error_log_dir=log_dir,
            ),
            log_level="warning",
        )
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    deadline = time.monotonic() + ENGINE_START_WAIT_SECONDS
    while time.monotonic() < deadline:
        if server.started:
            return server, thread, int(listener.getsockname()[1])
        time.sleep(ENGINE_START_POLL_SECONDS)
    server.should_exit = True
    thread.join(timeout=ENGINE_STOP_WAIT_SECONDS)
    raise RuntimeError(
        f"production engine did not start within {ENGINE_START_WAIT_SECONDS} s"
    )


def stop_engine(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=ENGINE_STOP_WAIT_SECONDS)


def persisted_client_error_rows(log_dir: Path) -> list[dict[str, object]]:
    """Every row the engine durably wrote, across every daily log in ``log_dir``."""
    rows: list[dict[str, object]] = []
    for path in sorted(log_dir.glob("webui-client-errors-*.log")):
        rows.extend(
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        )
    return rows


class ReplyDelayingProxy:
    """Forward every request to ``upstream_port`` at once; hold ONE reply.

    The request reaches the engine immediately and the engine's reply is read
    in full before the delay starts, so a held ``POST`` is one the engine has
    ALREADY recorded. That ordering is the point: it is the case in which a
    client that gives up and posts again writes the same trend twice.

    Only the FIRST reply to ``slow_method slow_path`` is held. A second
    request to the same route is answered at once, so a retry is not hidden
    behind the same delay.
    """

    def __init__(
        self, upstream_port: int, *, slow_method: str, slow_path: str, delay_seconds: float
    ) -> None:
        self.forwarded: list[tuple[str, str]] = []
        self._upstream_port = upstream_port
        self._slow = (slow_method, slow_path)
        self._delay_seconds = delay_seconds
        self._lock = threading.Lock()
        self._held_once = False
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                proxy._relay(self, body=None)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                proxy._relay(self, body=self.rfile.read(length))

            def log_message(self, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer((LOOPBACK, 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _relay(self, handler: BaseHTTPRequestHandler, *, body: bytes | None) -> None:
        route = (handler.command, handler.path)
        upstream = http.client.HTTPConnection(
            LOOPBACK, self._upstream_port, timeout=PROXY_UPSTREAM_TIMEOUT_SECONDS
        )
        try:
            headers = {"Connection": "close"}
            content_type = handler.headers.get("Content-Type")
            if content_type is not None:
                headers["Content-Type"] = content_type
            upstream.request(handler.command, handler.path, body=body, headers=headers)
            reply = upstream.getresponse()
            status = reply.status
            payload = reply.read()
        finally:
            upstream.close()
        with self._lock:
            self.forwarded.append(route)
            hold = route == self._slow and not self._held_once
            if hold:
                self._held_once = True
        if hold:
            time.sleep(self._delay_seconds)
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
        except OSError:
            # The client stopped waiting for the held reply. That is a result
            # the test reads from the probe's side, not a proxy failure.
            return

    def count(self, method: str, path: str) -> int:
        with self._lock:
            return self.forwarded.count((method, path))

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


#: A health check that never answers.
STALL_HEALTH_SILENT: str = "health-silent"
#: Health answers; the POST is accepted and never answered.
STALL_POST_SILENT: str = "post-silent"
#: Health answers; the POST reply STARTS and then arrives one byte at a time,
#: every byte well inside any per-read socket timeout.
STALL_POST_DRIP: str = "post-drip"
STALL_MODES: tuple[str, ...] = (STALL_HEALTH_SILENT, STALL_POST_SILENT, STALL_POST_DRIP)

DRIP_INTERVAL_SECONDS: float = 0.1
#: A declared body length the drip never reaches inside the ceiling.
DRIP_DECLARED_BYTES: int = 10_000


class StallingEngine:
    """An endpoint that accepts a request and never finishes answering it.

    ``hold_seconds`` is the test's own ceiling, not the behavior under test:
    after it the connection is dropped, so a probe with NO bound of its own
    fails late and visibly instead of hanging the suite forever.
    """

    def __init__(self, mode: str, *, hold_seconds: float) -> None:
        if mode not in STALL_MODES:
            raise ValueError(f"unknown stall mode {mode!r}; modes are {STALL_MODES}")
        self.post_count = 0
        self._lock = threading.Lock()
        stalling = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if mode == STALL_HEALTH_SILENT:
                    time.sleep(hold_seconds)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(HEALTH_BODY)))
                self.end_headers()
                self.wfile.write(HEALTH_BODY)

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with stalling._lock:
                    stalling.post_count += 1
                if mode == STALL_POST_SILENT:
                    time.sleep(hold_seconds)
                elif mode == STALL_POST_DRIP:
                    self._drip_until(time.monotonic() + hold_seconds)
                else:
                    raise AssertionError(f"POST reached a {mode!r} engine")

            def _drip_until(self, give_up_at: float) -> None:
                try:
                    self.send_response(202)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(DRIP_DECLARED_BYTES))
                    self.end_headers()
                    self.wfile.flush()
                    while time.monotonic() < give_up_at:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(DRIP_INTERVAL_SECONDS)
                except OSError:
                    return

            def log_message(self, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer((LOOPBACK, 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


__all__ = [
    "CLIENT_ERRORS_PATH",
    "HEALTH_PATH",
    "STALL_HEALTH_SILENT",
    "STALL_MODES",
    "STALL_POST_DRIP",
    "STALL_POST_SILENT",
    "ReplyDelayingProxy",
    "StallingEngine",
    "persisted_client_error_rows",
    "serving_engine",
    "stop_engine",
]
