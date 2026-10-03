"""Real-engine harness for the CLOUDSYNC-14 CLI live-deck gate tests.

Split out of `test_cli_sync_live_deck_gate.py` (PR #3831, quality ratchet: the file
crossed the 600-line limit). Boots the ACTUAL production engine
(`apps.engine_core.app.create_app`) over real loopback HTTP with a real `EngineLock`,
plus two network-level instruments that fabricate no API surface: a bare hanging TCP
listener and a byte relay that delays one path in front of the real engine.

[if] a CLOUDSYNC-14 test needs an engine [then] it gets the real production app, not a
hand-written stand-in, [else stop].

-Claude
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from apps.engine_core.app import create_app
from apps.engine_core.config import EngineConfig
from apps.engine_core.lock import EngineLock
from tests.waits import start_uvicorn_in_thread

BOOT_ID = "test-boot-cloudsync14"


def free_port() -> int:
    """A port nothing is bound to right now. Only for URLs that must find NOTHING
    listening: the port is released on return, so a server started on it races every
    other process picking a port in that window. Servers use `bound_loopback`.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def bound_loopback() -> tuple[socket.socket, int]:
    """A socket already bound to an OS-chosen loopback port, plus that port.

    A server handed this socket (uvicorn ``sockets=[...]``) serves on a port no
    other process can take in between. `free_port` released the port before the
    server bound it, and main push CI run 37119163338 lost that race: the real
    engine died with "address already in use" on 127.0.0.1:45673.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return sock, int(sock.getsockname()[1])


def start_hanging_listener() -> tuple[socket.socket, threading.Thread, int]:
    """A bare TCP listener that accepts a connection and then answers nothing,
    ever (Codex review, PR #3831, P1/BLOCKING, replacing a hand-rolled FastAPI
    ``/api/v1/health`` stub whose handler just ``await``ed a sleep).

    This implements NO part of the health API surface -- no route, no JSON
    body, not even HTTP framing. A client's request against it just never
    gets a response, which is indistinguishable at the wire level from what a
    genuinely wedged real engine (accepted the connection, then stalled
    before writing anything back) would produce. Only the identity-probe
    stage is ever reached in the test that uses this, so nothing here needs
    to claim to BE the engine's health endpoint at all.
    """
    listener, port = bound_loopback()
    listener.listen(1)

    def _accept_and_hang() -> None:
        try:
            conn, _addr = listener.accept()
        except OSError:
            return  # listener closed while waiting -- test is tearing down
        try:
            conn.settimeout(30.0)
            # Drain the request and keep the connection OPEN, answering
            # nothing, until the client gives up and closes its end (recv
            # returns b""). A single recv(1) returned on the request's first
            # byte and the socket closed at once, so the client saw a reset
            # (ReadError), not the timeout this listener exists to produce
            # (Sol, PR #3831, P1/BLOCKING, review comment 4108494701).
            while conn.recv(4096):
                pass
        except OSError:
            pass
        finally:
            conn.close()

    thread = threading.Thread(target=_accept_and_hang, name="hanging-listener", daemon=True)
    thread.start()
    return listener, thread, port


def start_path_delaying_proxy(
    *, upstream_port: int, delay_path: str, delay_s: float
) -> tuple[socket.socket, threading.Thread, int]:
    """A raw byte-forwarding TCP proxy in front of a REAL engine (Codex
    review, PR #3831, P1/BLOCKING; replaces a hand-rolled FastAPI stand-in
    that implemented BOTH ``/api/v1/health`` and ``/api/v1/state/ui-mirror``
    by hand).

    Every request's bytes are relayed to ``upstream_port`` verbatim and every
    response's bytes are relayed straight back, EXCEPT a request whose
    request line names ``delay_path``, which is held for ``delay_s`` before
    it is forwarded. This parses nothing but the request line (just enough
    to route the delay), constructs no response of its own, and never
    diverges from what the real engine actually answers -- the ONE thing it
    does that a real engine's own request handling gives no test seam for is
    delay one specific path's request without delaying the other, which is
    exactly what `test_verified_engine_mirror_timeout_fails_closed` needs
    and a raw byte relay is the narrowest way to get without adding a
    test-only delay to production code itself.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    proxy_port = int(listener.getsockname()[1])

    def _relay_one(client_conn: socket.socket) -> None:
        client_conn.settimeout(30.0)
        try:
            request = client_conn.recv(65536)
        except OSError:
            client_conn.close()
            return
        request_line = request.split(b"\r\n", 1)[0]
        if not request:
            client_conn.close()
            return
        if delay_path.encode("ascii") in request_line:
            time.sleep(delay_s)
        try:
            upstream = socket.create_connection(("127.0.0.1", upstream_port), timeout=5.0)
        except OSError:
            client_conn.close()
            return
        upstream.settimeout(30.0)
        try:
            upstream.sendall(request)
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                client_conn.sendall(chunk)
        except OSError:
            pass  # the client already gave up waiting -- nothing to relay to
        finally:
            upstream.close()
            client_conn.close()

    def _accept_loop() -> None:
        while True:
            try:
                conn, _addr = listener.accept()
            except OSError:
                return  # listener closed -- test is tearing down
            threading.Thread(target=_relay_one, args=(conn,), daemon=True).start()

    thread = threading.Thread(target=_accept_loop, name="delaying-proxy", daemon=True)
    thread.start()
    return listener, thread, proxy_port


def write_lock(data_dir: Path, *, port: int, host: str = "127.0.0.1") -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / ".engine.lock").write_text(
        json.dumps(
            {
                "pid": 1,
                "role": "opendj-engine",
                "boot_id": BOOT_ID,
                "host": host,
                "port": port,
            }
        ),
        encoding="utf-8",
    )


def boot_real_engine(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, *, listener: socket.socket
) -> tuple[FastAPI, uvicorn.Server, Any, EngineLock]:
    """Boot ``apps.engine_core.app.create_app`` -- the ACTUAL production
    engine, not a hand-rolled stand-in (Codex review, PR #3831,
    P1/BLOCKING) -- over real HTTP on loopback, with a real ``EngineLock``
    (the SAME class the shipped engine uses to write its own lock file, so
    ``/api/v1/health``'s ``boot_id`` genuinely matches what
    `_probe_engine_lock` reads back from disk).

    The acceptance tests in this module are the evidence CLOUDSYNC-14
    shipped, so they must exercise the production engine's real
    ``/api/v1/health`` and ``/api/v1/state/ui-mirror`` wiring, prefixes, and
    lifecycle -- not routes a test author re-typed by hand, which can
    silently agree with the client even when the real thing differs.

    ``listener`` comes from `bound_loopback`: the engine serves on that
    already-bound socket, so no other process can take its port first.

    Returns ``(app, server, thread, lock)``; the caller owns tearing all four
    down (``server.should_exit = True``, join the thread, ``lock.release()``).
    """
    port = int(listener.getsockname()[1])
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MDT_LIBRARY_MODE", "local")
    monkeypatch.setenv("MUSIC_DJ_BACKEND_PORT", str(port))
    monkeypatch.setenv("MUSIC_DJ_FRONTEND_PORT", str(free_port()))
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    lock = EngineLock(data_dir / ".engine.lock", host="127.0.0.1", port=port)
    lock.acquire()
    app = create_app(EngineConfig(data_dir=data_dir, port=port), lock=lock)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="the real engine", sockets=[listener])
    return app, server, thread, lock
