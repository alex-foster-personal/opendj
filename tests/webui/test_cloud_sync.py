"""Syncthing capability probe tests (cloud-sync-surface, CAT-04)."""
from __future__ import annotations

import json
import urllib.error

import pytest

from apps.webui.server import cloud_sync
from apps.webui.server.app import create_app


@pytest.mark.requirement("CAT-04")
def test_probe_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("SYNCTHING_API_URL", raising=False)
    monkeypatch.delenv("SYNCTHING_API_KEY", raising=False)
    assert cloud_sync.probe_syncthing_status() is None


@pytest.mark.requirement("CAT-04")
def test_probe_returns_none_on_unreachable_daemon(monkeypatch):
    monkeypatch.setenv("SYNCTHING_API_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("SYNCTHING_API_KEY", "test-key")

    def _raise(*_args, **_kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cloud_sync.urllib.request, "urlopen", _raise)
    assert cloud_sync.probe_syncthing_status() is None


@pytest.mark.requirement("CAT-04")
def test_probe_parses_connections_and_folder_state(monkeypatch):
    monkeypatch.setenv("SYNCTHING_API_URL", "http://127.0.0.1:8384")
    monkeypatch.setenv("SYNCTHING_API_KEY", "test-key")
    monkeypatch.setenv("SYNCTHING_FOLDER_ID", "music-library")

    responses = {
        "http://127.0.0.1:8384/rest/system/connections": {
            "connections": {
                "DEVICE-A": {"connected": True},
                "DEVICE-B": {"connected": False},
            }
        },
        "http://127.0.0.1:8384/rest/db/status?folder=music-library": {
            "state": "idle"
        },
    }

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

    def _fake_urlopen(request, timeout):
        return _FakeResponse(responses[request.full_url])

    monkeypatch.setattr(cloud_sync.urllib.request, "urlopen", _fake_urlopen)

    result = cloud_sync.probe_syncthing_status()
    assert result == {"peers_connected": 1, "folder_state": "idle"}


@pytest.mark.requirement("CAT-05")
def test_health_surfaces_configured_syncthing_status(seed_backend):
    app = create_app(
        backend=seed_backend, bind_host="127.0.0.1", hostname="test-host",
        syncthing_status_fn=lambda: {
            "peers_connected": 2, "folder_state": "syncing",
        },
    )
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        body = r.json()
        assert body["syncthing"] == {
            "peers_connected": 2, "folder_state": "syncing",
            "last_scan_at": None,
        }
