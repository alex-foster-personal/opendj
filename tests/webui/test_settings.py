"""Settings endpoint tests (settings-page, CAT-05)."""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.paths import STATE_DB


def _flatten(body: dict) -> dict[str, dict]:
    """{key: item} across all groups, for easy lookup in assertions."""
    out: dict[str, dict] = {}
    for group in body["groups"]:
        for item in group["items"]:
            out[item["key"]] = item
    return out


@pytest.mark.requirement("CAT-05")
def test_settings_happy_path(client):
    r = client.get("/api/v1/settings")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["groups"], list) and body["groups"]
    items = _flatten(body)
    assert items["bind_host"]["value"] == "127.0.0.1"
    assert items["hostname"]["value"] == "test-host"
    assert items["backend_type"]["value"] == "InMemoryBackend"
    assert items["version"]["tbd"] is False


@pytest.mark.requirement("CAT-05")
def test_settings_bind_host_reflects_override(insecure_client):
    r = insecure_client.get("/api/v1/settings")
    assert r.status_code == 200
    items = _flatten(r.json())
    assert items["bind_host"]["value"] == "0.0.0.0"


@pytest.mark.requirement("CAT-05")
def test_settings_port_reflects_effective_runtime_config(client):
    """The daemon reports the exact port passed into its application state."""
    r = client.get("/api/v1/settings")
    items = _flatten(r.json())
    assert items["port"]["tbd"] is False
    assert items["port"]["value"] == 18697


@pytest.mark.requirement("CAT-05")
def test_settings_cors_reflects_live_middleware(client):
    r = client.get("/api/v1/settings")
    items = _flatten(r.json())
    assert items["cors_enabled"]["value"] is True
    assert "http://localhost:19411" in items["cors_allow_origins"]["value"]


@pytest.mark.requirement("CAT-05")
def test_settings_storage_paths_are_strings(client):
    r = client.get("/api/v1/settings")
    items = _flatten(r.json())
    for key in ("data_dir", "state_dir", "anlz_cache_dir"):
        assert isinstance(items[key]["value"], str)
        assert items[key]["tbd"] is False


@pytest.mark.requirement("POLICY-01")
def test_settings_publish_runtime_policy_defaults(client):
    """[if] GET /api/v1/settings [then] runtime policy defaults are published, [else stop]."""
    response = client.get("/api/v1/settings")
    assert response.status_code == 200
    items = _flatten(response.json())
    assert items["hide_broken_playlist_min_available_ratio"]["value"] == 0.3
    assert items["anlz_points_default"]["value"] == 38400
    assert items["anlz_points_min"]["value"] == 100
    assert items["anlz_points_max"]["value"] == 38400
    assert items["file_exists_ttl_s"]["value"] == 30.0


@pytest.mark.requirement("CAT-05")
def test_settings_publish_vibe_runtime_defaults(client):
    response = client.get("/api/v1/settings")
    assert response.status_code == 200
    items = _flatten(response.json())
    assert items["vibe_sensitivity"]["value"] == 0.07
    assert items["vibe_decay_per_sec"]["value"] == 0.05


@pytest.mark.requirement("CAT-05")
def test_settings_feature_toggles_group_is_honest_tbd(client):
    """machine_perf_tier reports tbd on legacy webui without engine host-info."""
    r = client.get("/api/v1/settings")
    body = r.json()
    toggles = next(g for g in body["groups"] if g["group"] == "Feature toggles")
    note = next(i for i in toggles["items"] if i["key"] == "feature_toggles")["note"]
    assert "perf-tier" in note
    machine = next(i for i in toggles["items"] if i["key"] == "machine_perf_tier")
    assert machine["tbd"] is True
    assert "host-info" in machine["note"]


@pytest.mark.requirement("CAT-05")
def test_settings_in_openapi_schema(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert "/api/v1/settings" in r.json()["paths"]


@pytest.mark.requirement("CAT-05")
def test_settings_backend_type_reflects_sqlite_when_state_db_present(tmp_path):
    """Skip cleanly when the real state.db fixture isn't in this checkout.

    Cloud build sessions have no local daemon / real library (STATE_DB may
    not exist), so this only runs where a state.db is actually present --
    it never fabricates one.
    """
    if not Path(STATE_DB).is_file():
        pytest.skip(f"state.db not present at {STATE_DB}; skipping live-backend check")
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app
    from apps.webui.server.sqlite_backend import make_backend

    app = create_app(backend=make_backend(STATE_DB), bind_host="127.0.0.1")
    with TestClient(app) as c:
        r = c.get("/api/v1/settings")
    items = _flatten(r.json())
    assert items["backend_type"]["value"] == "SqliteBackend"

pytestmark = pytest.mark.rb_parity
