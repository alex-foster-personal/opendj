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
def test_health_process_env_forbidden_keys_names_a_leaked_prefix_never_a_value(client, monkeypatch):
    """if process_env_forbidden_keys omits a forbidden-prefixed var's NAME, or
    leaks its VALUE, then broken.

    OPS-32 round 3: the post-install rollout probe reads this field instead
    of reading procargs2 off the engine's pid directly (measured Mon 14 Sep
    2026 to never see the real bundled engine's env that way). Names only --
    a value here would be the exact leak OPS-32 exists to prevent.
    """
    monkeypatch.setenv("R2_OPS32_ROUTE_TEST_CANARY", "super-secret-value-should-not-leak")
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    forbidden = body["process_env_forbidden_keys"]
    assert isinstance(forbidden, list)
    assert all(isinstance(k, str) for k in forbidden)
    assert "R2_OPS32_ROUTE_TEST_CANARY" in forbidden
    assert body["process_env_home_present"] is True
    assert "super-secret-value-should-not-leak" not in r.text


@pytest.mark.requirement("OPS-32")
def test_health_never_discloses_a_benign_non_forbidden_env_name(client, monkeypatch):
    """if a real, non-forbidden-prefix env var NAME appears anywhere in the
    health body then broken.

    OPS-32 round 4 (issue #2637/#2638 follow-on, Mon 14 Sep 2026): round 3's
    `process_env_keys` field listed EVERY env var name, and that field was
    readable by an UNAUTHENTICATED caller on a token-mode share host
    (`/api/v1/health` is exempt from apps/webui/server/share_gate.py's auth
    gate so cloudflared can probe it pre-auth). Narrowed to just the
    forbidden-prefixed names plus one control bit, so a caller who should
    never see this install's configuration cannot enumerate it. This is the
    disclosure regression test: a benign env var name that does NOT match
    any forbidden prefix must not surface anywhere in the raw response,
    checked at the raw-text level so a future field cannot reintroduce the
    leak under a different key name.
    """
    monkeypatch.setenv("OPS32_BENIGN_CANARY", "irrelevant-value")
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert "OPS32_BENIGN_CANARY" not in r.text


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
