"""Transport timeout classification and elapsed-time errors (CSSTATUS-10)."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from apps.cloud.asset_store import AssetStoreError, _read_presigned_body
from apps.sync_hub.transport import (
    PUSH_TIMEOUT_S,
    HttpTransport,
    SyncTransportError,
    classify_transport_failure,
)


@contextmanager
def _hanging_http_server() -> Iterator[str]:
    """A real listen socket that accepts and never writes a response."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    held: list[socket.socket] = []
    stop = threading.Event()

    def _accept_and_hold() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except (TimeoutError, OSError):
                continue
            held.append(conn)

    thread = threading.Thread(target=_accept_and_hold, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}"
    finally:
        stop.set()
        thread.join(timeout=2)
        for conn in held:
            conn.close()
        server.close()


@pytest.mark.requirement("CSSTATUS-10")
def test_classify_transport_failure_includes_elapsed_for_502() -> None:
    """[if] a 502 transport failure is classified [then] its kind is proxy timeout, [else stop]."""
    classified = classify_transport_failure(
        "POST https://hub:8870/api/v1/sync/push -> HTTP 502:  (after 31.2s)"
    )
    assert classified is not None
    assert classified.kind == "proxy_or_hub_timeout"
    assert classified.elapsed_s == 31.2
    assert "502" in classified.headline


def test_classify_transport_failure_client_timeout() -> None:
    classified = classify_transport_failure(
        "POST https://hub/push client timeout after 120.0s: timed out"
    )
    assert classified is not None
    assert classified.kind == "client_timeout"
    assert classified.elapsed_s == 120.0


def test_http_transport_records_elapsed_on_url_timeout() -> None:
    """Connect-time URLError(timeout) still maps to SyncTransportError.

    Driven by a real listen socket that accepts and stays silent, which on
    some stacks wraps as URLError and on others as bare TimeoutError.
    """
    with _hanging_http_server() as base:
        transport = HttpTransport(base, timeout_s=0.4, push_timeout_s=0.4)
        with pytest.raises(SyncTransportError) as excinfo:
            transport.post("/api/v1/sync/push", {})
    assert "client timeout after" in str(excinfo.value)
    classified = classify_transport_failure(str(excinfo.value))
    assert classified is not None
    assert classified.kind == "client_timeout"


@pytest.mark.requirement("CSSTATUS-10")
def test_http_transport_maps_mid_read_socket_timeout() -> None:
    """[if] hub accepts then stalls [then] transport raises classified timeout, [else stop]."""
    with _hanging_http_server() as base:
        transport = HttpTransport(base, timeout_s=0.4, push_timeout_s=0.4)
        with pytest.raises(SyncTransportError) as excinfo:
            transport.get("/api/v1/sync/hello", {})
    message = str(excinfo.value)
    assert "client timeout after" in message
    assert "TimeoutError" not in type(excinfo.value).__name__
    classified = classify_transport_failure(message)
    assert classified is not None
    assert classified.kind == "client_timeout"
    assert classified.elapsed_s is not None
    assert classified.elapsed_s >= 0.4


@pytest.mark.requirement("STEM-27")
def test_presigned_get_maps_mid_read_socket_timeout() -> None:
    """[if] presigned GET stalls mid-read [then] AssetStoreError, [else stop]."""
    digest = "0" * 64
    with _hanging_http_server() as base:
        with pytest.raises(AssetStoreError, match="presigned GET unreachable") as excinfo:
            _read_presigned_body(f"{base}/assets/{digest}", digest, timeout_s=0.4)
    assert isinstance(excinfo.value, AssetStoreError)
    assert not isinstance(excinfo.value, TimeoutError)


def test_push_timeout_default_is_longer_than_hello() -> None:
    assert PUSH_TIMEOUT_S > 30.0
