"""Production application wiring contract for the existing Sets router."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app

_LEGACY_OPENAPI_PATHS = (
    "/api/sets",
    "/api/sets/recorder",
    "/api/sets/recorder/start",
    "/api/sets/recorder/{session_id}/stop",
    "/api/sets/recorder/{session_id}/recover",
    "/api/sets/{session_id}/timeline",
    "/api/sets/{session_id}/audio/{segment}",
    "/api/sets/{session_id}/share",
    "/api/sets/{session_id}/soundcloud-export",
)

_V1_OPENAPI_PATHS = tuple("/api/v1/sets" + path.removeprefix("/api/sets") for path in _LEGACY_OPENAPI_PATHS)


def test_create_app_registers_session_rec_http_contract() -> None:
    schema = create_app(mount_frontend=False).openapi()

    for path in _LEGACY_OPENAPI_PATHS:
        assert path in schema["paths"]
    for path in _V1_OPENAPI_PATHS:
        assert path in schema["paths"]

    export_path = "/api/sets/{session_id}/soundcloud-export"
    assert "get" in schema["paths"][export_path]
    assert "post" in schema["paths"][export_path]
    v1_export = "/api/v1/sets/{session_id}/soundcloud-export"
    assert "get" in schema["paths"][v1_export]
    assert "post" in schema["paths"][v1_export]


@pytest.mark.requirement("SET-11818")
def test_v1_sets_list_not_shadowed_by_tracks_stable_id() -> None:
    """[if] GET /api/v1/sets runs [then] it lists sessions not a track, [else stop]."""
    client = TestClient(create_app(mount_frontend=False))
    response = client.get("/api/v1/sets")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert "stable_id" not in body if body else True


@pytest.mark.requirement("SET-11818")
def test_v1_and_legacy_recorder_share_one_service() -> None:
    """[if] both prefixes are idle [then] GET /recorder JSON matches, [else stop]."""
    client = TestClient(create_app(mount_frontend=False))
    legacy = client.get("/api/sets/recorder")
    v1 = client.get("/api/v1/sets/recorder")
    assert legacy.status_code == 200
    assert v1.status_code == 200
    assert legacy.json() == v1.json()


pytestmark = pytest.mark.rb_parity
