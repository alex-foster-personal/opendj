"""Health endpoint + bind-warning + lock-holder tests (CAT-05, CAT-05b)."""
from __future__ import annotations

import pytest


@pytest.mark.requirement("CAT-05")
def test_health_happy_path(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["bind_host"] == "127.0.0.1"
    assert body["state_db"]["tracks"] == 5
    assert body["state_db"]["playlists"] == 2
    assert body["state_db"]["pairings"] == 1
    assert body["cloud"]["lock_holder"] is None
    waveform = body["waveform_materialization"]
    assert waveform["selected"] in {"rust-pyo3", "python-numpy"}
    assert waveform["native_available"] == (waveform["selected"] == "rust-pyo3")
    assert waveform["native_import_error"] is None or waveform["selected"] == "python-numpy"
    assert body["version"]


@pytest.mark.requirement("CAT-05")
def test_health_surfaces_lock_holder(locked_client):
    r = locked_client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["cloud"]["lock_holder"]["holder"] == "other-host"


@pytest.mark.requirement("CAT-05")
def test_health_no_bind_warning_on_localhost(client):
    r = client.get("/api/v1/health")
    assert "X-Bind-Warning" not in r.headers


@pytest.mark.requirement("CAT-05")
def test_health_adds_bind_warning_on_non_localhost(insecure_client):
    r = insecure_client.get("/api/v1/health")
    assert r.status_code == 200
    assert "X-Bind-Warning" in r.headers


@pytest.mark.requirement("CAT-05")
def test_openapi_schema_generated(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert schema["info"]["title"] == "music-dj-tools webui"
    paths = schema["paths"]
    for path in (
        "/api/v1/tracks", "/api/v1/tracks/{stable_id}",
        "/api/v1/playlists", "/api/v1/playlists/{playlist_id}",
        "/api/v1/pairings", "/api/v1/pairings/{pairing_id}",
        "/api/v1/queues/{kind}", "/api/v1/health",
    ):
        assert path in paths, f"missing: {path}"
