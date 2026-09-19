"""Production SPA routing regressions."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import apps.webui.server.app as app_module
from apps.webui.server.backend import InMemoryBackend


@pytest.fixture
def production_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Serve a minimal built frontend through the production app wiring."""
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "index.html").write_text(
        "<html><body>rekordbox parity</body></html>", encoding="utf-8",
    )
    assets_dir = build_dir / "assets"
    assets_dir.mkdir()
    (assets_dir / "app.js").write_text("window.appReady = true;", encoding="utf-8")
    immutable_dir = build_dir / "_app" / "immutable" / "assets"
    immutable_dir.mkdir(parents=True)
    (immutable_dir / "x.abc123.js").write_text(
        "window.hashedChunk = true;", encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "FRONTEND_BUILD_DIR", build_dir)

    app = app_module.create_app(backend=InMemoryBackend())
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("path", ["/performance", "/performance/preload1"])
def test_extensionless_client_routes_serve_spa_index(
    production_client: TestClient, path: str,
) -> None:
    response = production_client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "rekordbox parity" in response.text


def test_api_routes_take_precedence_over_spa(production_client: TestClient) -> None:
    response = production_client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("path", ["/api/not-real", "/api/v1/not-real"])
def test_unknown_api_route_remains_json_404(
    production_client: TestClient, path: str,
) -> None:
    response = production_client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize(
    ("path", "expected"),
    [(r"api\v1\not-real", False), (r"performance\preload1", True)],
)
def test_windows_mounted_paths_are_classified_correctly(
    path: str, expected: bool,
) -> None:
    assert app_module._SpaStaticFiles._is_client_route(path) is expected


def test_missing_asset_remains_404(production_client: TestClient) -> None:
    response = production_client.get("/assets/missing.js")

    assert response.status_code == 404
    assert "rekordbox parity" not in response.text


def test_existing_asset_is_served(production_client: TestClient) -> None:
    response = production_client.get("/assets/app.js")

    assert response.status_code == 200
    assert response.text == "window.appReady = true;"


@pytest.mark.parametrize("path", ["/", "/index.html", "/performance"])
def test_html_entry_responses_are_not_cached(
    production_client: TestClient, path: str,
) -> None:
    response = production_client.get(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_hashed_immutable_asset_is_cached_forever(
    production_client: TestClient,
) -> None:
    response = production_client.get("/_app/immutable/assets/x.abc123.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"

pytestmark = pytest.mark.rb_parity
