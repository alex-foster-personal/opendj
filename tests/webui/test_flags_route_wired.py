"""Legacy GET /api/v1/flags wiring (issue #5789).

- if the legacy webui app stops serving GET /api/v1/flags then the store-build store 404s on every load
"""

from __future__ import annotations

from tests.webui.app_fixtures import client_fixture  # noqa: F401  (pytest fixtures)


def test_legacy_app_serves_flags(client):
    response = client.get("/api/v1/flags")
    assert response.status_code == 200
    body = response.json()
    assert {"flags", "build_profile", "sandboxed"} <= body.keys()
    assert isinstance(body["flags"], list) and body["flags"]
