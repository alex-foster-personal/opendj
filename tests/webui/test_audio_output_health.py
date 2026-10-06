"""AUDIO-DEVICE-01 shell output-health proxy (issue #923)."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.shell_output_health import ShellOutputHealthClient


class _FakeShellClient:
    def __init__(self, get_payload: dict, post_payload: dict | None = None) -> None:
        self._get_payload = get_payload
        self._post_payload = post_payload or {
            "cycled": True,
            "from": "A",
            "via": "B",
            "restored": "A",
        }

    def get_output_health(self) -> dict:
        return self._get_payload

    def post_switch_output(self) -> dict:
        return self._post_payload


@pytest.mark.requirement("AUDIO-DEVICE-01")
def test_get_unknown_when_shell_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] no .engine.shell.json [then] GET returns verdict unknown, [else stop]."""
    from apps.webui.server.routes import audio_output_health as routes

    fake = _FakeShellClient(
        {
            "device_delivering": None,
            "verdict": "unknown",
            "reason": "desktop shell health port unavailable (.engine.shell.json missing)",
            "default_device_name": None,
            "default_device_uid": None,
            "io_cycles_advanced": None,
            "hal_overload_recent": None,
            "probe_available": False,
            "checked_at": "2026-09-20T00:00:00.000Z",
        }
    )

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")

    def _override() -> _FakeShellClient:
        return fake

    app.dependency_overrides[routes._client] = _override
    with TestClient(app) as client:
        response = client.get("/api/v1/audio/output-health")
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "unknown"
    assert body["probe_available"] is False


@pytest.mark.requirement("AUDIO-DEVICE-01")
def test_get_passes_through_shell_not_delivering(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] fake shell returns device_delivering false [then] engine passes it on, [else stop]."""
    from apps.webui.server.routes import audio_output_health as routes

    fake = _FakeShellClient(
        {
            "device_delivering": False,
            "verdict": "not_delivering",
            "reason": "stuck",
            "default_device_name": "Sony",
            "default_device_uid": "uid-sony",
            "io_cycles_advanced": False,
            "hal_overload_recent": False,
            "probe_available": True,
            "checked_at": "2026-09-20T00:00:00.000Z",
        }
    )

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[routes._client] = lambda: fake
    with TestClient(app) as client:
        response = client.get("/api/v1/audio/output-health")
    assert response.status_code == 200
    assert response.json()["device_delivering"] is False
    assert response.json()["verdict"] == "not_delivering"


@pytest.mark.requirement("AUDIO-DEVICE-01")
def test_shell_client_unknown_without_shell_json(tmp_path: Path) -> None:
    """[if] shell json missing [then] client returns unknown not an exception, [else stop]."""
    client = ShellOutputHealthClient(data_dir=tmp_path)
    payload = client.get_output_health()
    assert payload["verdict"] == "unknown"
    assert payload["probe_available"] is False


@pytest.mark.requirement("AUDIO-DEVICE-01")
def test_post_switch_output_503_when_shell_refuses(tmp_path: Path) -> None:
    """[if] the shell refuses to cycle the output [then] POST returns 503, [else stop]."""
    from apps.webui.server.routes import audio_output_health as routes

    fake = _FakeShellClient(
        {"verdict": "ok", "device_delivering": True, "probe_available": True, "checked_at": "t"},
        post_payload={"cycled": False, "error": "only one output device"},
    )

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[routes._client] = lambda: fake
    with TestClient(app) as client:
        response = client.post("/api/v1/audio/switch-output")
    assert response.status_code == 503


@pytest.mark.requirement("AUDIO-DEVICE-01")
def test_shell_client_reads_health_port(tmp_path: Path) -> None:
    """[if] .engine.shell.json names a health_port [then] the client reads it, [else stop]."""
    shell_json = tmp_path / ".engine.shell.json"
    shell_json.write_text(json.dumps({"health_port": 59999, "shell_pid": 1}), encoding="utf-8")
    client = ShellOutputHealthClient(data_dir=tmp_path)
    assert client.read_health_port() == 59999


@pytest.mark.requirement("AUDIO-DEVICE-01")
def test_cli_health_exits_no_engine_when_lock_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] audio_output_health has no engine [then] it exits 2 naming the lock, [else stop]."""
    from apps.opendj_cli import EXIT_NO_ENGINE
    from apps.opendj_cli.__main__ import main
    from apps.webui import port_config

    monkeypatch.delenv("MUSIC_DJ_BACKEND_PORT", raising=False)
    monkeypatch.delenv("MUSIC_DJ_FRONTEND_PORT", raising=False)
    monkeypatch.setattr(port_config, "WEBUI_ENV_FILE", tmp_path / "no.env")
    missing = tmp_path / "absent" / ".engine.lock"
    assert main(["--lock", str(missing), "audio_output_health"]) == EXIT_NO_ENGINE
    assert str(missing) in capsys.readouterr().err


# ---------------------------------------------------------------------------
# AUDIO-DEVICE-02: a shell that times out is a 503 with a named reason, and a
# jack-muted speaker is reported as such, never as "ok".


def _serve_once(respond: bool) -> tuple[int, threading.Event]:
    """A loopback server that accepts one connection and either answers a
    real JSON body or holds the socket open without a byte (a stalled shell)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    done = threading.Event()

    def _run() -> None:
        conn, _ = listener.accept()
        try:
            conn.recv(4096)
            if respond:
                body = json.dumps(
                    {"verdict": "ok", "device_delivering": True, "probe_available": True, "checked_at": "t"}
                ).encode()
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + body
                )
            else:
                done.wait(5)
        finally:
            conn.close()
            listener.close()

    threading.Thread(target=_run, daemon=True).start()
    return port, done


def _shell_client_on(tmp_path: Path, port: int, monkeypatch: pytest.MonkeyPatch) -> ShellOutputHealthClient:
    from apps.webui.server import shell_output_health

    monkeypatch.setattr(shell_output_health.sys, "platform", "darwin")
    monkeypatch.setattr(shell_output_health, "SHELL_TIMEOUT_S", 0.3)
    (tmp_path / ".engine.shell.json").write_text(json.dumps({"health_port": port}), encoding="utf-8")
    return ShellOutputHealthClient(data_dir=tmp_path)


@pytest.mark.requirement("AUDIO-DEVICE-02")
def test_shell_client_raises_a_named_timeout_for_a_stalled_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the shell health server accepts but never answers [then] the client raises ShellHealthTimeout, not TimeoutError, [else stop]."""
    from apps.webui.server.shell_output_health import ShellHealthTimeout

    port, release = _serve_once(respond=False)
    client = _shell_client_on(tmp_path, port, monkeypatch)
    try:
        with pytest.raises(ShellHealthTimeout, match="timed out"):
            client.get_output_health()
    finally:
        release.set()


@pytest.mark.requirement("AUDIO-DEVICE-02")
def test_control_shell_client_passes_a_prompt_answer_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the shell answers within the timeout [then] its payload comes back unchanged, [else stop]."""
    port, _ = _serve_once(respond=True)
    payload = _shell_client_on(tmp_path, port, monkeypatch).get_output_health()
    assert payload["verdict"] == "ok"


class _TimingOutShellClient:
    def get_output_health(self) -> dict:
        from apps.webui.server.shell_output_health import ShellHealthTimeout

        raise ShellHealthTimeout("GET /api/v1/audio/output-health timed out after 3.0s: timed out")

    def post_switch_output(self) -> dict:
        return self.get_output_health()


def _app_with(fake: object) -> FastAPI:
    from apps.webui.server.routes import audio_output_health as routes

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[routes._client] = lambda: fake
    return app


@pytest.mark.requirement("AUDIO-DEVICE-02")
def test_route_maps_a_shell_timeout_to_503_shell_health_timeout() -> None:
    """[if] the shell times out [then] GET and POST answer 503 with reason shell_health_timeout, never 500, [else stop]."""
    with TestClient(_app_with(_TimingOutShellClient()), raise_server_exceptions=False) as client:
        got = client.get("/api/v1/audio/output-health")
        posted = client.post("/api/v1/audio/switch-output")
    for response in (got, posted):
        assert response.status_code == 503, response.text
        detail = response.json()["detail"]
        assert detail["reason"] == "shell_health_timeout"
        assert detail["verdict"] == "unknown"


@pytest.mark.requirement("AUDIO-DEVICE-02")
def test_route_passes_muted_by_jack_and_the_master_fault_through() -> None:
    """[if] the shell reports muted_by_jack with a master fault [then] the response keeps both, [else stop]."""
    fault = {
        "kind": "muted_by_jack",
        "uid": "BuiltInSpeakerDevice",
        "stage": "released",
        "message": "MASTER and CUE can't share the MacBook's built-in output",
    }
    fake = _FakeShellClient(
        {
            "device_delivering": True,
            "verdict": "muted_by_jack",
            "reason": "speakers muted by the jack",
            "default_device_name": "MacBook Pro Speakers",
            "default_device_uid": "BuiltInSpeakerDevice",
            "io_cycles_advanced": True,
            "hal_overload_recent": True,
            "probe_available": True,
            "default_muted_by_jack": True,
            "master_pin_fault": fault,
            "checked_at": "2026-10-06T02:18:32.793Z",
        }
    )
    with TestClient(_app_with(fake)) as client:
        body = client.get("/api/v1/audio/output-health").json()
    assert body["verdict"] == "muted_by_jack"
    assert body["default_muted_by_jack"] is True
    assert body["master_pin_fault"] == fault


@pytest.mark.requirement("AUDIO-DEVICE-02")
def test_cli_prints_the_master_pin_fault_line() -> None:
    """[if] the health payload carries a master fault [then] the CLI's text line names its kind and message, [else stop]."""
    from apps.opendj_cli.audio_output_health_cli import _fault_line

    assert _fault_line(None) == "none"
    assert _fault_line({"kind": "overridden_by_system", "stage": "released", "message": "m"}) == (
        "overridden_by_system (released): m"
    )
    with pytest.raises(TypeError):
        _fault_line("muted")


@pytest.mark.requirement("AUDIO-DEVICE-02")
def test_control_a_refused_connection_is_unknown_not_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] nothing listens on the shell port [then] the client answers verdict unknown, not ShellHealthTimeout, [else stop]."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    payload = _shell_client_on(tmp_path, closed_port, monkeypatch).get_output_health()
    assert payload["verdict"] == "unknown"
    assert "request failed" in payload["reason"]
