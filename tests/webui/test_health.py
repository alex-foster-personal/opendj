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
    assert isinstance(body["google_oauth_configured"], bool)


@pytest.mark.requirement("CAT-05")
def test_health_google_oauth_configured_false_when_env_missing(client, monkeypatch):
    for name in (
        "OPENDJ_GOOGLE_OAUTH_CLIENT_ID",
        "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["google_oauth_configured"] is False


@pytest.mark.requirement("CAT-05")
def test_health_google_oauth_configured_true_when_env_set(client, monkeypatch):
    monkeypatch.setenv("OPENDJ_GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["google_oauth_configured"] is True


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


@pytest.mark.requirement("OPS-32")
def test_health_process_env_keys_lists_names_never_values(client, monkeypatch):
    """if process_env_keys omits a set var's NAME, or leaks its VALUE, then broken.

    OPS-32 round 3: the post-install rollout probe reads this field instead
    of reading procargs2 off the engine's pid directly (measured Mon 14 Sep
    2026 to never see the real bundled engine's env that way). Names only --
    a value here would be the exact leak OPS-32 exists to prevent.
    """
    monkeypatch.setenv("OPS32_ROUTE_TEST_CANARY", "super-secret-value-should-not-leak")
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    keys = body["process_env_keys"]
    assert isinstance(keys, list)
    assert all(isinstance(k, str) for k in keys)
    assert "OPS32_ROUTE_TEST_CANARY" in keys
    assert "HOME" in keys
    assert "super-secret-value-should-not-leak" not in r.text


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

pytestmark = pytest.mark.rb_parity
