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
def test_settings_port_is_tbd_not_fabricated(client):
    """The daemon cannot introspect its own --port; must say so, not guess."""
    r = client.get("/api/v1/settings")
    items = _flatten(r.json())
    assert items["port"]["tbd"] is True
    assert items["port"]["value"] is None


@pytest.mark.requirement("CAT-05")
def test_settings_cors_reflects_live_middleware(client):
    r = client.get("/api/v1/settings")
    items = _flatten(r.json())
    assert items["cors_enabled"]["value"] is True
    assert "http://localhost:5173" in items["cors_allow_origins"]["value"]


@pytest.mark.requirement("CAT-05")
def test_settings_storage_paths_are_strings(client):
    r = client.get("/api/v1/settings")
    items = _flatten(r.json())
    for key in ("data_dir", "state_dir", "anlz_cache_dir"):
        assert isinstance(items[key]["value"], str)
        assert items[key]["tbd"] is False


@pytest.mark.requirement("CAT-05")
def test_settings_feature_toggles_group_is_honest_tbd(client):
    """No env-driven feature toggles exist yet; must not fabricate one."""
    r = client.get("/api/v1/settings")
    body = r.json()
    toggles = next(g for g in body["groups"] if g["group"] == "Feature toggles")
    assert all(item["tbd"] for item in toggles["items"])


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
    from apps.webui.server.app import create_app
    from apps.webui.server.sqlite_backend import make_backend
    from fastapi.testclient import TestClient

    app = create_app(backend=make_backend(STATE_DB), bind_host="127.0.0.1")
    with TestClient(app) as c:
        r = c.get("/api/v1/settings")
    items = _flatten(r.json())
    assert items["backend_type"]["value"] == "SqliteBackend"
