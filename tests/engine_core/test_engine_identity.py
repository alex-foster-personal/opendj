"""Hermetic tests for engine lock identity verification (issue #3042)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from apps.shared import engine_origin
from apps.shared.engine_origin import (
    EngineIdentityMismatch,
    EngineNotRunning,
    EngineOrigin,
    resolve_origin,
    verify_engine_identity,
)


def _write_lock(lock_path: Path, **overrides: object) -> EngineOrigin:
    payload: dict[str, object] = {
        "pid": 4242,
        "role": "opendj-engine",
        "host": "127.0.0.1",
        "port": 9433,
        "boot_id": "lock-boot",
    }
    payload.update(overrides)
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    return resolve_origin(lock_path)


def _mock_health(monkeypatch: pytest.MonkeyPatch, boot_id: str) -> None:
    class _Response:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {"boot_id": boot_id, "status": "ok"}

    class _Client:
        def __init__(self, timeout: float, trust_env: bool) -> None:
            # The probe must never route through an environment proxy (Sol
            # review, PR #3831, P1/BLOCKING).
            assert trust_env is False
            self.timeout = timeout

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str) -> _Response:
            return _Response()

    monkeypatch.setattr("apps.shared.engine_origin.httpx.Client", _Client)


def test_boot_id_mismatch_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = tmp_path / ".engine.lock"
    origin = _write_lock(lock_path, port=9433)
    _mock_health(monkeypatch, "health-boot")

    with pytest.raises(EngineIdentityMismatch) as excinfo:
        verify_engine_identity(origin)

    message = str(excinfo.value)
    assert str(lock_path) in message
    assert "lock-boot" in message
    assert "health-boot" in message
    assert "9433" in message


def test_wrong_role_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = tmp_path / ".engine.lock"
    origin = _write_lock(lock_path, role="not-opendj-engine")
    called = False

    class _Response:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {"boot_id": "lock-boot"}

    class _Client:
        def __init__(self, timeout: float, trust_env: bool) -> None:
            # The probe must never route through an environment proxy (Sol
            # review, PR #3831, P1/BLOCKING).
            assert trust_env is False
            self.timeout = timeout

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str) -> _Response:
            nonlocal called
            called = True
            return _Response()

    monkeypatch.setattr("apps.shared.engine_origin.httpx.Client", _Client)

    with pytest.raises(EngineIdentityMismatch) as excinfo:
        verify_engine_identity(origin)

    message = str(excinfo.value)
    assert "not-opendj-engine" in message
    assert "opendj-engine" in message
    assert called is False


def test_non_engine_responder_refuses_without_html(tmp_path: Path) -> None:
    lock_path = tmp_path / ".engine.lock"
    server = HTTPServer(("127.0.0.1", 0), _Html404Handler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = _write_lock(lock_path, port=port)

    try:
        with pytest.raises(EngineNotRunning) as excinfo:
            verify_engine_identity(origin)
        message = str(excinfo.value).lower()
        assert "<html" not in message
        assert "not found" not in message
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_matching_identity_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = tmp_path / ".engine.lock"
    origin = _write_lock(lock_path)
    _mock_health(monkeypatch, "lock-boot")
    verify_engine_identity(origin)


class _Html404Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(404)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body>Not Found</body></html>")

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.mark.requirement("ENGLOCK-6443")
def test_resolve_origin_retries_an_unparsable_lock_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    [if] resolve_origin sees spaces once [then] it retries and returns origin, [else stop].
    """
    lock_path = tmp_path / ".engine.lock"
    valid = json.dumps(
        {
            "pid": 1,
            "role": "opendj-engine",
            "host": "127.0.0.1",
            "port": 8585,
            "boot_id": "boot-a",
        }
    )
    reads = iter(["   ", valid])
    read_count = 0

    def _read_text(self: Path, encoding: str = "utf-8") -> str:
        nonlocal read_count
        if self == lock_path:
            read_count += 1
            return next(reads, valid)
        return Path.read_text(self, encoding=encoding)

    lock_path.write_text(valid, encoding="utf-8")
    monkeypatch.setattr(Path, "read_text", _read_text)
    monkeypatch.setattr(engine_origin.time, "sleep", lambda _s: None)
    origin = resolve_origin(lock_path)
    assert origin.port == 8585
    assert read_count == 2


@pytest.mark.requirement("ENGLOCK-6443")
def test_resolve_origin_gives_up_after_the_retry_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    [if] every read is unparsable [then] EngineNotRunning names invalid JSON, [else stop].
    """
    lock_path = tmp_path / ".engine.lock"
    lock_path.write_text("   ", encoding="utf-8")
    monkeypatch.setattr(engine_origin.time, "sleep", lambda _s: None)
    with pytest.raises(EngineNotRunning) as refusal:
        resolve_origin(lock_path)
    message = str(refusal.value)
    assert "is not valid JSON" in message
    assert str(lock_path) in message
