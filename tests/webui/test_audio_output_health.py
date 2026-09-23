"""AUDIO-DEVICE-01 shell output-health proxy (issue #923)."""

from __future__ import annotations

import json
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
